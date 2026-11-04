"""Уровень 9: план, разбитый на задачи с зависимостями.

Задача = шаг плана под управлением runtime: id, описание, зависимости,
состояние и проверяемый результат. Runtime запускает задачу только после
успешного завершения её зависимостей (последовательного исполнения достаточно).

Ключевое правило: одного текстового заявления модели недостаточно.
Задачу закрывает только сам runtime — инструмент complete_task запускает
команду проверки из плана, и при совпадении кода завершения пишет
task_completed. Служебные проверки не считаются бюджетом агента:
это механизм runtime, а не действие модели (см. README).
"""

from __future__ import annotations

import json

from agent import events
from agent.tools.base import (
    INVALID_ARGUMENTS,
    TASK_DEPENDENCIES_NOT_MET,
    VERIFICATION_FAILED,
    VERIFICATION_REQUIRED,
    ToolContext,
    ToolError,
)
from agent.tools.bash import run_bash

VERIFY_OUTPUT_CAP = 4_000  # лимит вывода служебной проверки (символы)


def say(text: str) -> None:
    print(text, flush=True)


def print_checklist(state) -> None:
    """Чеклист задач в stdout — требование уровня 9."""
    if not state.tasks:
        return
    say("[чеклист]")
    for task in state.tasks.values():
        mark = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}[task.status]
        say(f"  {mark} {task.id} {task.description}")


def on_plan_approved(state, journal) -> None:
    """Создаёт задачи из шагов плана и запускает первую доступную."""
    if not state.plan:
        return
    payload = [
        {
            "id": step["id"],
            "description": step["description"],
            "depends_on": step.get("depends_on", []),
            "verification": step.get("verification"),
        }
        for step in state.plan["steps"]
    ]
    journal.append(events.TASKS_CREATED, tasks=payload)
    say(f"  · план разбит на {len(payload)} задач")
    start_next_task(state, journal)


def start_next_task(state, journal) -> str | None:
    """Запускает следующую задачу с выполненными зависимостствами."""
    task = state.next_eligible_task()
    if task is None:
        if state.tasks and not state.open_tasks():
            journal.append(
                events.USER_NOTE,
                text="Все задачи выполнены и подтверждены проверками. "
                "Заверши работу кратким текстовым итогом.",
            )
        print_checklist(state)
        return None
    journal.append(events.TASK_STARTED, task_id=task.id)
    say(f"▶ задача {task.id}: {task.description}")
    verification = (task.verification or {}).get("command", "—")
    journal.append(
        events.USER_NOTE,
        text=(
            f"Текущая задача {task.id}: {task.description}. "
            f"Проверка готовности: `{verification}`. "
            "Выполни задачу инструментами и вызови complete_task — "
            "runtime сам запустит проверку."
        ),
    )
    print_checklist(state)
    return task.id


def run_verification(cfg, verification: dict) -> dict:
    """Служебный запуск команды проверки. Неtool-вызов: без списания бюджета."""
    command = verification["command"]
    expected = verification.get("expect_exit_code", 0)
    ctx = ToolContext(
        workspace=cfg.workspace,
        max_output_bytes=VERIFY_OUTPUT_CAP,
        bash_timeout=cfg.bash_timeout,
        bash_path=cfg.bash_path,
        protected_paths=(),
    )
    try:
        payload = run_bash({"command": command, "cwd": "."}, ctx)
        exit_code = payload["exit_code"]
        output = payload["stdout"] + payload["stderr"]
        timed_out = payload.get("timed_out", False)
    except ToolError as err:  # например, таймаут проверки
        exit_code = None
        output = err.message
        timed_out = True
    return {
        "command": command,
        "expect_exit_code": expected,
        "exit_code": exit_code,
        "ok": exit_code == expected,
        "timed_out": timed_out,
        "output": output[-800:],
    }


def complete_task(state, journal, cfg, task_id: str, call_id: str, attempt: int,
                  override_command: str | None = None) -> None:
    """Обработка complete_task: проверка результата самим runtime.

    Команда проверки берётся из плана; если плановая команда сама по себе
    ошибочна, модель может передать исправленную через override_command —
    исполняет и фиксирует код завершения всё равно runtime.
    Журналирует tool_call_finished с результатом проверки; при успехе —
    task_completed и запуск следующей задачи.
    """
    task = state.tasks.get(task_id)
    if task is None:
        _finish_with_error(
            journal, call_id, attempt, INVALID_ARGUMENTS,
            f"неизвестная задача '{task_id}'",
        )
        return
    if task.status == "completed":
        # Уже завершённая задача не перезапускается и не перепроверяется
        # (в том числе после resume) — возвращаем сохранённый результат.
        result = {
            "ok": True,
            "data": {
                "task_id": task_id,
                "already_completed": True,
                "verification": task.verification_result,
            },
        }
        _finish(journal, call_id, attempt, result)
        say(f"  · задача {task_id} уже завершена ранее — без повторной проверки")
        return
    if task.status == "pending":
        unmet = [d for d in task.depends_on if state.tasks[d].status != "completed"]
        _finish_with_error(
            journal, call_id, attempt, TASK_DEPENDENCIES_NOT_MET,
            f"задача {task_id} ждёт завершения зависимостей: {', '.join(unmet)}",
        )
        return
    verification_spec = dict(task.verification or {})
    if override_command:
        verification_spec["command"] = override_command
    if not verification_spec.get("command"):
        _finish_with_error(
            journal, call_id, attempt, VERIFICATION_REQUIRED,
            f"у задачи {task_id} нет проверяемого результата (verification.command "
            "в плане); спланируй задачи с командами проверки",
        )
        return

    verification = run_verification(cfg, verification_spec)
    if verification["ok"]:
        result = {"ok": True, "data": {"task_id": task_id, "verification": verification}}
        _finish(journal, call_id, attempt, result)
        journal.append(events.TASK_COMPLETED, task_id=task_id, verification=verification)
        say(f"✓ задача {task_id} подтверждена (exit_code={verification['exit_code']})")
        start_next_task(state, journal)
        return

    output_tail = (verification.get("output") or "").strip()[-200:]
    message = (
        f"проверка задачи {task_id} не пройдена: `{verification['command']}` "
        f"завершилась с кодом {verification['exit_code']}"
    )
    if output_tail:
        message += f"; вывод команды: {output_tail}"
    error = ToolError(
        VERIFICATION_FAILED,
        message,
        recoverable=True,
        data={"task_id": task_id, "verification": verification},
    )
    _finish(journal, call_id, attempt, error.envelope())
    say(f"⚠ задача {task_id}: проверка не пройдена (exit_code={verification['exit_code']})")


def _finish(journal, call_id: str, attempt: int, result: dict) -> None:
    size = len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    journal.append(
        events.TOOL_CALL_FINISHED,
        id=call_id,
        attempt=attempt,
        ok=bool(result.get("ok")),
        size=size,
        result=result,
    )


def _finish_with_error(journal, call_id, attempt, code: str, message: str) -> None:
    result = {"ok": False, "error": {"code": code, "message": message, "recoverable": True}}
    _finish(journal, call_id, attempt, result)
    say(f"  ⚠ {code}: {message[:90]}")
