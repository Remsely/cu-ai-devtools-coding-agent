"""Plan Mode: схема плана и её валидация.

План — структурированные данные (блок 3): цель, шаги в предполагаемом
порядке, инструменты каждого шага, риски/допущения и критерий готовности.
Сохраняется событием plan_saved вместе с run_id и ждёт явного решения
пользователя (approve/reject); исполнение начинается только после
утверждения. Шаги с полями depends_on/verification заготовлены под
уровень 9 (задачи с зависимостями и проверяемым результатом).

Сам инструмент submit_plan описан в agent/tools/plan_tool.py, чтобы
не создавать цикл импортов tools <-> planning.
"""

from __future__ import annotations

PLAN_TOOL = "submit_plan"


def _str_list(value, field: str, errors: list[str]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        errors.append(f"{field}: должен быть списком строк")
        return []
    return value


def validate_plan(raw) -> tuple[dict | None, list[str]]:
    """Нормализует и проверяет план. Возвращает (план, список ошибок)."""
    errors: list[str] = []
    if not isinstance(raw, dict):
        return None, ["план должен быть JSON-объектом"]

    goal = raw.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        errors.append("goal: непустая строка с целью работы")

    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        errors.append("steps: непустой список шагов")
        steps_raw = []

    steps: list[dict] = []
    known_ids: set[str] = set()
    for index, step in enumerate(steps_raw, start=1):
        if not isinstance(step, dict):
            errors.append(f"шаг {index}: должен быть объектом")
            continue
        step_id = str(step.get("id") or f"t{index}")
        description = step.get("description")
        if not isinstance(description, str) or not description.strip():
            errors.append(f"шаг {step_id}: description — непустая строка")
        if step_id in known_ids:
            errors.append(f"шаг {index}: дублирующийся id '{step_id}'")
            continue
        deps = _str_list(step.get("depends_on"), f"шаг {step_id}: depends_on", errors)
        for dep in deps:
            if dep == step_id:
                errors.append(f"шаг {step_id}: зависимость от самого себя")
            elif dep not in known_ids:
                errors.append(
                    f"шаг {step_id}: зависимость от неизвестного шага '{dep}' "
                    "(зависимости ссылаются на предыдущие шаги)"
                )
        verification = step.get("verification")
        if verification is not None and (
            not isinstance(verification, dict)
            or not isinstance(verification.get("command"), str)
        ):
            errors.append(
                f"шаг {step_id}: verification должен быть "
                "{'command': str, 'expect_exit_code'?: int}"
            )
            verification = None
        steps.append(
            {
                "id": step_id,
                "description": description if isinstance(description, str) else "",
                "depends_on": deps,
                "tools": _str_list(step.get("tools"), f"шаг {step_id}: tools", errors),
                **({"verification": verification} if verification else {}),
            }
        )
        known_ids.add(step_id)

    if errors:
        return None, errors
    plan = {
        "goal": goal.strip(),
        "steps": steps,
        "risks": _str_list(raw.get("risks"), "risks", errors),
        "assumptions": _str_list(raw.get("assumptions"), "assumptions", errors),
        "done_criteria": str(raw.get("done_criteria") or "").strip(),
    }
    return plan, []


def plan_summary_lines(plan: dict) -> list[str]:
    lines = [f"Цель: {plan['goal']}"]
    for index, step in enumerate(plan["steps"], start=1):
        deps = f" (после: {', '.join(step['depends_on'])})" if step["depends_on"] else ""
        lines.append(f"  {index}. [{step['id']}] {step['description']}{deps}")
    for title, key in (("Риски", "risks"), ("Допущения", "assumptions")):
        for item in plan.get(key, []):
            lines.append(f"{title}: {item}")
    if plan.get("done_criteria"):
        lines.append(f"Критерий готовности: {plan['done_criteria']}")
    return lines
