"""Восстановление после сбоя (блок 2).

Правила:
- завершённое действие повторно не выполняется (replay сохранённых
  результатов по tool_call_id);
- tool_call_started без tool_call_finished = «неопределённый» вызов:
  молча не повторяется, пользователь выбирает retry / skip / abort;
- не начатые до сбоя вызовы той же пачки помечаются NOT_EXECUTED_AFTER_CRASH,
  иначе история осталась бы без role=tool сообщений для них;
- повтор write_file не меняет файл повторно (инструмент идемпотентен);
- решение пользователя сохраняется в журнал привязанным к конкретному
  вызову и хешу аргументов (согласие не переносится на другую операцию).
"""

from __future__ import annotations

import hashlib
import json

from agent import changes, events
from agent.loop import Orchestrator, say
from agent.state import RunState
from agent.tools.base import NOT_EXECUTED_AFTER_CRASH, UNDEFINED_SKIPPED

DECISIONS = ("retry", "skip", "abort")


def analyze(state: RunState) -> tuple[list[str], list[str]]:
    """Классифицирует незакрытые вызовы после сбоя.

    Возвращает (undefined, pending):
    undefined — начатые, но не завершённые (нужно решение пользователя);
    pending   — запрошенные моделью, но не начатые (помечаются автоматически).
    """
    undefined = [rec.id for rec in state.undefined_calls()]
    pending = [cid for cid in state.missing_tool_answers() if cid not in undefined]
    return undefined, pending


def mark_not_executed(orch: Orchestrator, call_ids: list[str]) -> None:
    for call_id in call_ids:
        result = {
            "ok": False,
            "error": {
                "code": NOT_EXECUTED_AFTER_CRASH,
                "message": "вызов не был выполнен до сбоя процесса; "
                "при необходимости запроси его заново",
                "recoverable": True,
            },
        }
        orch.journal.append(
            events.TOOL_CALL_REFUSED,
            id=call_id,
            name=orch.state.call_name(call_id),
            result=result,
        )
        say(f"  · вызов {call_id} не выполнялся до сбоя — помечен NOT_EXECUTED")


def apply_decision(orch: Orchestrator, call_id: str, choice: str) -> None:
    if choice not in DECISIONS:
        raise ValueError(f"неизвестное решение {choice!r}; варианты: {DECISIONS}")
    state = orch.state
    record = state.calls[call_id]
    args_hash = "sha256:" + hashlib.sha256(record.args_json.encode("utf-8")).hexdigest()[:16]
    orch.journal.append(
        events.DECISION_RECORDED,
        subject="tool_call",
        call_id=call_id,
        tool=record.name,
        args_hash=args_hash,
        choice=choice,
        decided_by="human",
        expires_after_use=True,
    )
    if choice == "retry":
        # Явный повтор неопределённого вызова — новая попытка того же tool call.
        # Какие инструменты безопасно повторять: read_* — да; write_file — да
        # (идемпотентен); Bash — нет (см. README, «Повторяемость инструментов»).
        # Уровень 10: эффект вызова (если дерево изменилось) относится к
        # запуску решением пользователя — иначе retry упёрся бы в конфликт дерева.
        if record.side_effect:
            changes.absorb_undefined_effect(state, orch.journal, orch.cfg, call_id)
        say(f"  · retry {call_id} ({record.name}) — новая попытка")
        orch.retry_call(call_id)
        return
    if choice == "skip":
        # Уровень 10: изменившие файлы эффекты пропущенного вызова фиксируем
        # в журнале изменений (attribution=undefined_call), чтобы откат
        # оставался возможным.
        if record.side_effect:
            changes.absorb_undefined_effect(state, orch.journal, orch.cfg, call_id)
        result = {
            "ok": False,
            "error": {
                "code": UNDEFINED_SKIPPED,
                "message": "выполнение прервано сбоем; шаг по решению пользователя "
                "считаем неуспешным",
                "recoverable": True,
            },
        }
        size = len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
        orch.journal.append(
            events.TOOL_CALL_FINISHED,
            id=call_id,
            attempt=record.attempts[-1].number,
            ok=False,
            size=size,
            result=result,
        )
        say(f"  · {call_id}: помечен неуспешным, работа продолжается")
        return
    # abort
    orch.journal.append(
        events.RUN_FINISHED,
        status="aborted",
        reason=f"пользователь завершил запуск после неопределённого вызова {call_id}",
    )
    say(f"  ✖ запуск завершён по решению пользователя (abort)")


def describe_undefined(orch: Orchestrator, call_id: str) -> None:
    """Печатает карточку неопределённого вызова для решения пользователя."""
    record = orch.state.calls[call_id]
    say("Неопределённый вызов (начат, результат не сохранён):")
    say(f"  tool_call_id: {record.id}")
    say(f"  инструмент:   {record.name}  (safely_repeatable по классификации README)")
    say(f"  аргументы:    {record.args_json[:300]}")
    # Уровень 10: сверяем дерево с последним сохранённым состоянием и
    # показываем обнаруженные изменения пользователю (требование задания).
    dirty = changes.conflicts(orch.state, orch.cfg)
    if dirty:
        say("  наблюдение:   дерево отличается от журнала: " + ", ".join(sorted(dirty))
            + " — при retry/skip эти изменения будут отнесены к данному запуску")
    else:
        say("  наблюдение:   дерево совпадает с журналом (изменений нет)")
    if record.name == "write_file":
        try:
            args = json.loads(record.args_json)
            target = orch.cfg.workspace / args["path"]
            if target.exists():
                matches = (
                    target.read_text(encoding="utf-8", errors="replace") == args.get("content")
                )
                say(
                    "  наблюдение:   файл существует и "
                    + ("СОВПАДАЕТ с целевым содержимым (изменение, вероятно, применилось)"
                       if matches
                       else "ОТЛИЧАЕТСЯ от целевого содержимого")
                )
            else:
                say("  наблюдение:   целевого файла пока нет (изменение, вероятно, не применилось)")
        except (OSError, ValueError, KeyError):
            pass
