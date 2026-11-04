"""CLI: подкоманды run / resume / approve / reject / status.

Интерфейс — обычные подкоманды + минимальные вопросы пользователю там,
где этого требует задание (утверждение плана, решение по неопределённому
вызову). Каждое решение дублируется флагом, чтобы автотесты и демо
прогонялись неинтерактивно и детерминированно.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agent import changes, events, recovery
from agent.config import Config, Limits
from agent.events import Journal, new_run_id
from agent.llm import OpenAICompatModel, ScriptedModel
from agent.loop import FailpointCrash, Orchestrator, say
from agent.planning import plan_summary_lines
from agent.state import TERMINAL_STATUSES, RunState
from agent.tools import make_tools  # noqa: F401 — реестр для встраивания/расширения

REPO_ROOT = Path(__file__).resolve().parent.parent

EXIT_CODES = {
    "completed": 0,
    "awaiting_approval": 0,
    "limit_reached": 3,
    "aborted": 4,
    "failed": 5,
}

TASK = "Исправь расчёт итоговой стоимости заказа, чтобы тесты проходили. " \
       "Тесты не меняй. Сам найди нужные файлы, внеси исправление и запусти тесты."

SYSTEM_PROMPT = """\
Ты — консольный coding-агент. Ты работаешь с небольшим проектом в workspace \
и решаешь задачу, вызывая инструменты. Инструменты выбирай сам; заверши работу \
текстовым ответом, когда задача решена и подтверждена. Ошибки инструментов не \
страшны: читай код ошибки (error.code) и выбирай другой шаг. Тесты проекта \
(test_order.py) и файлы вне workspace не изменяй.

Режим планирования: сначала изучи проект инструментами list_files и read_file, \
затем вызови submit_plan с планом вида {"goal": str, "steps": [{"id": "t1", \
"description": str, "depends_on": [id], "tools": [имена], "verification": \
{"command": str, "expect_exit_code": 0}}], "risks": [str], "assumptions": [str], \
"done_criteria": str}. У каждого шага обязателен verification.command — bash-команда \
проверки (например: test -f order.py; python -c "assert ..."; python -m unittest -v). \
После submit_plan дождись утверждения плана пользователем.

Режим исполнения: работай по текущей задаче из плана; закрывай шаг вызовом \
complete_task(task_id) — runtime сам запустит проверку шага и не поверит \
голословному «готово». Тесты запускай через Bash из корня workspace: \
python -m unittest -v. Отвечай кратко, по-русски, без тега <think>. /no_think\
"""


# ------------------------------------------------------------------ helpers

def initial_messages(task: str, plan: bool) -> list[dict]:
    if plan:
        user = (
            f"Задача: {task}\n\n"
            "Режим планирования: сначала изучи проект инструментами чтения "
            "(list_files, read_file), затем вызови submit_plan со структурированным "
            "планом. Инструменты изменения файлов и Bash на этом этапе недоступны."
        )
    else:
        user = f"Задача: {task}"
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def start_run(cfg: Config, task: str, plan_mode: bool) -> tuple[RunState, Journal]:
    run_id = new_run_id()
    journal = Journal(cfg.runs_dir / run_id / "journal.jsonl")
    journal.append(
        events.RUN_STARTED,
        run_id=run_id,
        task=task,
        mode="plan" if plan_mode else "direct",
        workspace=str(cfg.workspace),
        limits=cfg.limits.as_dict(),
        initial_messages=initial_messages(task, plan=plan_mode),
    )
    state = RunState.replay(Journal.load(journal.path), run_id)
    return state, journal


def load_run(runs_dir: Path, run_id: str) -> tuple[RunState, Journal, Config]:
    journal_path = runs_dir / run_id / "journal.jsonl"
    if not journal_path.exists():
        raise SystemExit(f"запуск {run_id} не найден в {runs_dir}")
    state = RunState.replay(Journal.load(journal_path), run_id)
    # Конфигурация восстанавливается из журнала: лимиты и workspace привязаны
    # к run_id и не зависят от текущего окружения.
    cfg = Config.from_env(
        Path(state.workspace),
        runs_dir,
        limits=Limits(**state.limits.as_dict()),
    )
    return state, Journal(journal_path), cfg


def build_model(args) -> object:
    if getattr(args, "script", None):
        script = json.loads(Path(args.script).read_text(encoding="utf-8"))
        skip = getattr(args, "script_skip", 0) or 0
        return ScriptedModel(script[skip:])
    return OpenAICompatModel.from_env()


def _ask(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        return ""


def _print_plan(state: RunState) -> None:
    if not state.plan:
        return
    say("План (сохранён с run_id):")
    for line in plan_summary_lines(state.plan):
        say("    " + line)


def _exit_code(status: str) -> int:
    return EXIT_CODES.get(status, 1)


def _drive(orch: Orchestrator, args) -> int:
    """Прогоняет цикл, обрабатывая остановки на ожидании утверждения плана."""
    try:
        status = orch.run()
    except FailpointCrash:
        say(
            f"[сбой] процесс остановлен управляемо; состояние — в журнале. "
            f"Продолжение: python -m agent resume {orch.state.run_id}"
        )
        return 70
    if status == "awaiting_approval":
        _print_plan(orch.state)
        if getattr(args, "auto_approve", False):
            orch.approve_plan()
            return _drive(orch, args)
        if not getattr(args, "no_input", False):
            choice = _ask("Утвердить план? [y]es / [n]o: ").strip().lower()
            if choice in ("y", "yes", "д", "да"):
                orch.approve_plan()
                return _drive(orch, args)
            orch.reject_plan("отклонён пользователем в интерактивном режиме")
            return _exit_code("aborted")
        say(f"Ожидание решения: python -m agent approve {orch.state.run_id} | reject {orch.state.run_id}")
        return _exit_code("awaiting_approval")
    return _exit_code(status)


def _ask_decision(orch: Orchestrator, call_id: str) -> str:
    recovery.describe_undefined(orch, call_id)
    while True:
        choice = _ask("Действие: [r]etry / [s]kip / [a]bort: ").strip().lower()
        if choice in ("r", "retry", "п"):
            return "retry"
        if choice in ("s", "skip", "с"):
            return "skip"
        if choice in ("a", "abort", "з"):
            return "abort"
        say("Введите r, s или a.")


# ------------------------------------------------------------------ commands

def cmd_run(args) -> int:
    cfg = Config.from_env(
        Path(args.workspace).resolve(),
        Path(args.runs_dir).resolve(),
        Limits.from_json(args.limits),
        failpoint=args.failpoint,
    )
    state, journal = start_run(cfg, args.task, plan_mode=args.plan)
    say(f"Запуск {state.run_id}: workspace={cfg.workspace}, режим="
        + ("планирование" if args.plan else "прямое исполнение"))
    orch = Orchestrator(state, journal, build_model(args), cfg)
    return _drive(orch, args)


def cmd_resume(args) -> int:
    state, journal, cfg = load_run(Path(args.runs_dir).resolve(), args.run_id)
    if state.status in TERMINAL_STATUSES:
        say(f"Запуск {args.run_id} уже завершён: {state.status}"
            + (f" ({state.stop_reason})" if state.stop_reason else ""))
        return _exit_code(state.status)
    orch = Orchestrator(state, journal, build_model(args), cfg)

    undefined, pending = recovery.analyze(state)
    if pending:
        recovery.mark_not_executed(orch, pending)
    for call_id in undefined:
        choice = args.decision or _ask_decision(orch, call_id)
        recovery.apply_decision(orch, call_id, choice)
        if orch.state.status == "aborted":
            return _exit_code("aborted")
    return _drive(orch, args)


def cmd_approve(args) -> int:
    state, journal, cfg = load_run(Path(args.runs_dir).resolve(), args.run_id)
    if state.status != "awaiting_approval":
        raise SystemExit(f"утверждение невозможно: статус запуска {state.status}")
    orch = Orchestrator(state, journal, build_model(args), cfg)
    orch.approve_plan()
    return _drive(orch, args)


def cmd_reject(args) -> int:
    state, journal, cfg = load_run(Path(args.runs_dir).resolve(), args.run_id)
    if state.status != "awaiting_approval":
        raise SystemExit(f"отклонение невозможно: статус запуска {state.status}")
    Orchestrator(state, journal, build_model(args), cfg).reject_plan(args.reason or "")
    return _exit_code("aborted")


def cmd_status(args) -> int:
    state, _, cfg = load_run(Path(args.runs_dir).resolve(), args.run_id)
    say(f"run_id:      {state.run_id}")
    say(f"задача:      {state.task}")
    say(f"режим/фаза:  {state.mode} / {state.phase}")
    say(f"статус:      {state.status}" + (f" ({state.stop_reason})" if state.stop_reason else ""))
    say("бюджет:      " + _budget_line(state))
    if state.plan:
        _print_plan(state)
    if state.tasks:
        import agent.tasks as tasks_mod

        tasks_mod.print_checklist(state)
    if state.changes:
        affected = ", ".join(sorted(changes.aggregate_changes(state)))
        say(f"изменения:   {affected}")
    if state.rolled_back:
        say("откат:       выполнен")
    if state.final_answer:
        say(f"ответ:       {state.final_answer}")
    for record in state.calls.values():
        note = ""
        if record.attempts:
            note = f" попыток: {len(record.attempts)}, последняя: {record.attempts[-1].status}"
        elif record.refused:
            note = " отказ: " + record.refused["result"]["error"]["code"]
        task_note = f", задача {record.task_id}" if record.task_id else ""
        say(f"  call {record.id}: {record.name}{task_note}{note}")
    for decision in state.decisions:
        say(f"  решение: {decision['call_id']} → {decision['choice']}")
    return 0


def cmd_tasks(args) -> int:
    """Просмотр состояния задач (уровень 9)."""
    state, _, _ = load_run(Path(args.runs_dir).resolve(), args.run_id)
    if not state.tasks:
        say("У этого запуска нет задач (план не разбивался на задачи).")
        return 0
    import agent.tasks as tasks_mod

    tasks_mod.print_checklist(state)
    for task in state.tasks.values():
        verification = (task.verification or {}).get("command", "—")
        result = task.verification_result or {}
        extra = (
            f"проверка: `{verification}` → exit_code={result.get('exit_code')}"
            if task.status == "completed"
            else f"проверка: `{verification}`"
        )
        say(f"  {task.id} [{task.status}] {task.description} ({extra})")
    return 0


def cmd_diff(args) -> int:
    """Просмотр изменений запуска (уровень 10)."""
    state, _, cfg = load_run(Path(args.runs_dir).resolve(), args.run_id)
    for line in changes.diff_report(state, cfg):
        say(line)
    return 0


def cmd_rollback(args) -> int:
    """Откат изменений запуска по явной команде (уровень 10)."""
    state, journal, cfg = load_run(Path(args.runs_dir).resolve(), args.run_id)
    try:
        changes.rollback(state, journal, cfg)
    except changes.ChangeConflict as err:
        say(f"[откат] конфликт: {err}")
        return 6
    return 0


def _budget_line(state) -> str:
    c, l = state.counters, state.limits
    return (
        f"model_requests {c.model_requests}/{l.model_requests}, "
        f"tool_calls {c.tool_calls}/{l.tool_calls}, "
        f"side_effects {c.side_effects}/{l.side_effects}, "
        f"output_bytes {c.output_bytes}/{l.output_bytes}"
    )


# ------------------------------------------------------------------- parser

def _add_model_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--script", default=None,
                        help="JSON-файл сценария scripted-модели (для тестов/демо)")
    parser.add_argument("--script-skip", type=int, default=0,
                        help="пропустить первые N реплик сценария (для resume)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent",
        description="Учебный coding agent: agent loop, восстановление, бюджеты, Plan Mode",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="новый запуск агента")
    run_p.add_argument("--task", default=TASK, help="задача для агента")
    run_p.add_argument("--workspace", default=str(REPO_ROOT / "workspace"))
    run_p.add_argument("--runs-dir", default=str(REPO_ROOT / "runs"))
    run_p.add_argument("--plan", action="store_true", help="режим планирования (Plan Mode)")
    run_p.add_argument("--auto-approve", action="store_true",
                       help="утвердить план без вопроса (для тестов)")
    run_p.add_argument("--no-input", action="store_true", help="не задавать вопросов")
    run_p.add_argument("--limits", default=None, help='JSON лимитов, напр. {"tool_calls": 5}')
    run_p.add_argument("--failpoint", default=None, choices=["after_start", "after_execute"],
                       help="управляемая точка отказа для демонстрации сбоя")
    _add_model_args(run_p)
    run_p.set_defaults(func=cmd_run)

    resume_p = sub.add_parser("resume", help="продолжить запуск после сбоя")
    resume_p.add_argument("run_id")
    resume_p.add_argument("--runs-dir", default=str(REPO_ROOT / "runs"))
    resume_p.add_argument("--decision", choices=list(recovery.DECISIONS), default=None,
                          help="решение по неопределённому вызову: retry/skip/abort")
    resume_p.add_argument("--auto-approve", action="store_true")
    resume_p.add_argument("--no-input", action="store_true")
    _add_model_args(resume_p)
    resume_p.set_defaults(func=cmd_resume)

    approve_p = sub.add_parser("approve", help="утвердить план и начать исполнение")
    approve_p.add_argument("run_id")
    approve_p.add_argument("--runs-dir", default=str(REPO_ROOT / "runs"))
    _add_model_args(approve_p)
    approve_p.set_defaults(func=cmd_approve)

    reject_p = sub.add_parser("reject", help="отклонить план")
    reject_p.add_argument("run_id")
    reject_p.add_argument("--runs-dir", default=str(REPO_ROOT / "runs"))
    reject_p.add_argument("--reason", default="")
    _add_model_args(reject_p)
    reject_p.set_defaults(func=cmd_reject)

    status_p = sub.add_parser("status", help="состояние запуска")
    status_p.add_argument("run_id")
    status_p.add_argument("--runs-dir", default=str(REPO_ROOT / "runs"))
    status_p.set_defaults(func=cmd_status)

    tasks_p = sub.add_parser("tasks", help="состояние задач запуска (уровень 9)")
    tasks_p.add_argument("run_id")
    tasks_p.add_argument("--runs-dir", default=str(REPO_ROOT / "runs"))
    tasks_p.set_defaults(func=cmd_tasks)

    diff_p = sub.add_parser("diff", help="изменения запуска (уровень 10)")
    diff_p.add_argument("run_id")
    diff_p.add_argument("--runs-dir", default=str(REPO_ROOT / "runs"))
    diff_p.set_defaults(func=cmd_diff)

    rollback_p = sub.add_parser("rollback", help="откатить изменения запуска (уровень 10)")
    rollback_p.add_argument("run_id")
    rollback_p.add_argument("--runs-dir", default=str(REPO_ROOT / "runs"))
    rollback_p.set_defaults(func=cmd_rollback)

    return parser


def main(argv=None) -> int:
    from agent.config import load_env_file

    load_env_file()  # .env из корня репозитория, если есть
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except FailpointCrash:
        # Точка отказа вне _drive (например, в retry при resume)
        say("[сбой] процесс остановлен управляемо (failpoint в resume)")
        return 70


if __name__ == "__main__":
    sys.exit(main())
