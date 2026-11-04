"""Инструмент complete_task (уровень 9)."""

from __future__ import annotations

from agent.tools.base import Param, Tool

COMPLETE_TASK_TOOL = "complete_task"


def _complete_task(args: dict, ctx) -> dict:
    # Проверка результата и смена статуса задачи — в agent/tasks.py (loop);
    # сам инструмент побочных эффектов не имеет.
    return {"task_id": args.get("task_id")}


COMPLETE_TASK = Tool(
    name=COMPLETE_TASK_TOOL,
    description=(
        "Сообщает runtime, что текущая задача выполнена. Runtime сам запускает "
        "команду проверки этой задачи из плана и только при совпадении кода "
        "завершения помечает задачу выполненной. Текстовое заявление модели "
        "результатом не считается. Если команда проверки из плана сама по себе "
        "ошибочна (синтаксис и т.п.), передай исправленную в аргументе "
        "verification_command — runtime запустит её вместо плановой."
    ),
    params=(
        Param("task_id", "string", "Идентификатор задачи из плана (например, t2)"),
        Param("verification_command", "string",
              "Необязательная исправленная bash-команда проверки вместо плановой",
              required=False),
    ),
    run_fn=_complete_task,
    side_effect=False,
    safely_repeatable=True,  # завершённая задача не перепроверяется
)
