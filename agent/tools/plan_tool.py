"""Инструмент submit_plan: сохранение плана (блок 3).

Валидация и журналирование выполняются в loop (нужен журнал);
сам инструмент побочных эффектов не имеет.
"""

from __future__ import annotations

from agent.planning import PLAN_TOOL
from agent.tools.base import Param, Tool


def _submit_plan(args: dict, ctx) -> dict:
    return {"plan": args.get("plan")}


SUBMIT_PLAN = Tool(
    name=PLAN_TOOL,
    description=(
        "Сохраняет план работы (структурированный JSON) и переводит запуск "
        "в состояние ожидания утверждения пользователем. Доступен только "
        "в режиме планирования. Схема: {goal, steps: [{id?, description, "
        "depends_on?, tools?, verification?}], risks?, assumptions?, done_criteria}"
    ),
    params=(
        Param("plan", "object", "План работы — JSON-объект по схеме из описания"),
    ),
    run_fn=_submit_plan,
    side_effect=False,
    safely_repeatable=True,
)
