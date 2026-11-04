"""Общие фикстуры тестов: учебный workspace и конфигурация запуска."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

ORDER_BUGGY = (
    "def calculate_total(prices: list[float], discount_percent: float) -> float:\n"
    "    return sum(prices) - discount_percent\n"
)

ORDER_TEST = """import unittest

from order import calculate_total


class OrderTest(unittest.TestCase):
    def test_discount(self) -> None:
        self.assertEqual(calculate_total([100, 50], 10), 135)

    def test_invalid_discount(self) -> None:
        for value in (-1, 101):
            with self.assertRaises(ValueError):
                calculate_total([100], value)
"""

ORDER_FIXED = (
    "def calculate_total(prices: list[float], discount_percent: float) -> float:\n"
    "    if not 0 <= discount_percent <= 100:\n"
    "        raise ValueError(f\"discount_percent must be between 0 and 100, got {discount_percent}\")\n"
    "    return sum(prices) * (1 - discount_percent / 100)\n"
)

TASK = "Исправь расчёт итоговой стоимости заказа, чтобы тесты проходили. " \
       "Тесты не меняй. Сам найди нужные файлы, внеси исправление и запусти тесты."

PLAN = {
    "goal": "Исправить расчёт итоговой стоимости заказа так, чтобы тесты проходили",
    "steps": [
        {
            "id": "t1",
            "description": "Изучить workspace: order.py и test_order.py",
            "tools": ["list_files", "read_file"],
            "verification": {"command": "test -f order.py -a -f test_order.py"},
        },
        {
            "id": "t2",
            "description": "Исправить calculate_total: процентная скидка и валидация 0..100",
            "tools": ["write_file"],
            "depends_on": ["t1"],
            "verification": {
                "command": "python -c \"from order import calculate_total; "
                           "assert calculate_total([100, 50], 10) == 135; "
                           "assert calculate_total([100], 0) == 100\""
            },
        },
        {
            "id": "t3",
            "description": "Запустить тесты и убедиться, что они зелёные",
            "tools": ["Bash"],
            "depends_on": ["t2"],
            "verification": {"command": "python -m unittest -v"},
        },
    ],
    "risks": ["нельзя менять test_order.py"],
    "assumptions": ["скидка задаётся в процентах от суммы заказа"],
    "done_criteria": "python -m unittest -v завершается с кодом 0, test_order.py не изменён",
}

CHECKPOINT_SCRIPT = [
    {"tool_calls": [{"id": "c1", "name": "list_files", "arguments": {"path": "."}}]},
    {
        "tool_calls": [
            {"id": "c2", "name": "read_file", "arguments": {"path": "order.py"}},
            {"id": "c3", "name": "read_file", "arguments": {"path": "test_order.py"}},
        ]
    },
    {"tool_calls": [{"id": "c4", "name": "submit_plan", "arguments": {"plan": PLAN}}]},
    {"tool_calls": [{"id": "c5", "name": "complete_task", "arguments": {"task_id": "t1"}}]},
    {"tool_calls": [{"id": "c6", "name": "write_file",
                     "arguments": {"path": "order.py", "content": ORDER_FIXED}}]},
    {"tool_calls": [{"id": "c7", "name": "complete_task", "arguments": {"task_id": "t2"}}]},
    {"tool_calls": [{"id": "c8", "name": "Bash",
                     "arguments": {"command": "python -m unittest -v", "cwd": "."}}]},
    {"tool_calls": [{"id": "c9", "name": "complete_task", "arguments": {"task_id": "t3"}}]},
    {"text": "Исправлено: скидка считается в процентах, добавлена валидация диапазона. Тесты проходят."},
]


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    # newline="\n": содержимое байт-в-байт равно константам (хеши в тестах)
    (ws / "order.py").write_text(ORDER_BUGGY, encoding="utf-8", newline="\n")
    (ws / "test_order.py").write_text(ORDER_TEST, encoding="utf-8", newline="\n")
    return ws


@pytest.fixture
def runs_dir(tmp_path: Path) -> Path:
    return tmp_path / "runs"


def make_cfg(workspace: Path, runs_dir: Path, limits=None, failpoint: str | None = None):
    from agent.config import Config, Limits

    return Config.from_env(
        workspace,
        runs_dir,
        limits=limits or Limits(model_requests=20, tool_calls=40, side_effects=20,
                                output_bytes=200_000),
        failpoint=failpoint,
    )


def start_run(cfg, task: str, plan_mode: bool):
    from agent.cli import start_run as _start_run

    return _start_run(cfg, task, plan_mode)


def run_until_done(orch):
    """Прогоняет цикл, автоматически утверждая план (для тестов)."""
    status = orch.run()
    if status == "awaiting_approval":
        orch.approve_plan()
        status = orch.run()
    return status
