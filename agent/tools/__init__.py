"""Реестр инструментов и политика фаз.

Plan Mode (блок 3): в фазе планирования модели физически не передаются
инструменты записи и Bash — их нет в списке tools запроса, поэтому
побочный эффект до утверждения плана невозможен по конструкции,
а не по уговорам в промпте.
"""

from __future__ import annotations

from agent.tools.base import Tool
from agent.tools.bash import BASH
from agent.tools.fs import LIST_FILES, READ_FILE, WRITE_FILE
from agent.tools.plan_tool import SUBMIT_PLAN
from agent.tools.task_tool import COMPLETE_TASK, COMPLETE_TASK_TOOL

PLANNING_TOOLS = ("list_files", "read_file", "submit_plan")
EXECUTING_TOOLS = ("list_files", "read_file", "write_file", "Bash", "complete_task")


def make_tools() -> dict[str, Tool]:
    tools = (LIST_FILES, READ_FILE, WRITE_FILE, BASH, SUBMIT_PLAN, COMPLETE_TASK)
    registry = {t.name: t for t in tools}
    if len(registry) != len(tools):
        raise RuntimeError("дублирующиеся имена инструментов")
    return registry


def tools_for_phase(registry: dict[str, Tool], phase: str) -> list[dict]:
    names = PLANNING_TOOLS if phase == "planning" else EXECUTING_TOOLS
    return [registry[name].definition() for name in names]
