"""Тесты бюджета (уровень 8): пачки, исчерпание лимитов, обрезка вывода."""

from __future__ import annotations

from agent.budget import Counters, batch_refusal_reason, output_cap
from agent.config import Limits
from agent.events import Journal
from agent.llm import ScriptedModel
from agent.loop import Orchestrator

from conftest import TASK, make_cfg, start_run


# ------------------------------------------------------------------- unit

def test_batch_reason_tool_calls():
    counters = Counters(tool_calls=3)
    limits = Limits(tool_calls=5)
    assert batch_refusal_reason(counters, limits, [False, False, False]) == "tool_calls"


def test_batch_reason_side_effects():
    counters = Counters(side_effects=2)
    limits = Limits(side_effects=3)
    assert batch_refusal_reason(counters, limits, [True, True]) == "side_effects"


def test_batch_fits():
    assert batch_refusal_reason(Counters(), Limits(), [True, False]) is None


def test_output_cap_exhausted():
    counters = Counters(output_bytes=100)
    limits = Limits(output_bytes=100)
    assert output_cap(counters, limits) == 0
    assert batch_refusal_reason(counters, limits, [False]) == "output_bytes"


# ------------------------------------------------------------- через цикл

def test_model_request_limit(workspace, runs_dir):
    cfg = make_cfg(
        workspace, runs_dir,
        limits=Limits(model_requests=1, tool_calls=5, side_effects=5, output_bytes=50_000),
    )
    state, journal = start_run(cfg, TASK, plan_mode=False)
    script = [
        {"tool_calls": [{"id": "m1", "name": "read_file", "arguments": {"path": "order.py"}}]},
        {"text": "готово"},
    ]
    orch = Orchestrator(state, journal, ScriptedModel(script), cfg)
    assert orch.run() == "limit_reached"
    assert state.counters.model_requests == 1
    assert state.calls["m1"].status == "completed"
    assert state.stop_reason and "model_requests" in state.stop_reason


def test_batch_rejected_wholesale(workspace, runs_dir):
    """Не хватает на всю пачку — не выполняется ни один вызов."""
    cfg = make_cfg(
        workspace, runs_dir,
        limits=Limits(model_requests=10, tool_calls=2, side_effects=5, output_bytes=50_000),
    )
    state, journal = start_run(cfg, TASK, plan_mode=False)
    script = [
        {
            "tool_calls": [
                {"id": "b1", "name": "read_file", "arguments": {"path": "order.py"}},
                {"id": "b2", "name": "read_file", "arguments": {"path": "test_order.py"}},
                {"id": "b3", "name": "read_file", "arguments": {"path": "order.py"}},
            ]
        },
        {"text": "готово"},
    ]
    orch = Orchestrator(state, journal, ScriptedModel(script), cfg)
    assert orch.run() == "completed"
    started = [e for e in Journal.load(journal.path) if e["event"] == "tool_call_started"]
    assert started == []  # ни один вызов пачки не исполнялся и не списывался
    assert state.counters.tool_calls == 0
    for call_id in ("b1", "b2", "b3"):
        code = state.calls[call_id].refused["result"]["error"]["code"]
        assert code == "BUDGET_EXCEEDED"
    assert "tool_calls" in state.calls["b1"].refused["result"]["error"]["message"]


def test_side_effect_limit(workspace, runs_dir):
    cfg = make_cfg(
        workspace, runs_dir,
        limits=Limits(model_requests=10, tool_calls=10, side_effects=0, output_bytes=50_000),
    )
    state, journal = start_run(cfg, TASK, plan_mode=False)
    script = [
        {"tool_calls": [{"id": "s1", "name": "Bash", "arguments": {"command": "echo hi"}}]},
        {"text": "готово"},
    ]
    orch = Orchestrator(state, journal, ScriptedModel(script), cfg)
    assert orch.run() == "completed"
    assert state.calls["s1"].refused["result"]["error"]["code"] == "BUDGET_EXCEEDED"
    assert state.counters.side_effects == 0


def test_output_truncation_then_refusal(workspace, runs_dir):
    """Размер результата заранее неизвестен: остаток → инструменту, вывод
    обрезается с пометкой, следующий вызов при нулевом остатке не начинается."""
    (workspace / "big.txt").write_text("x" * 20_000, encoding="utf-8")
    cfg = make_cfg(
        workspace, runs_dir,
        limits=Limits(model_requests=10, tool_calls=10, side_effects=5, output_bytes=2_000),
    )
    state, journal = start_run(cfg, TASK, plan_mode=False)
    script = [
        {"tool_calls": [{"id": "r1", "name": "read_file", "arguments": {"path": "big.txt"}}]},
        {"tool_calls": [{"id": "r2", "name": "read_file", "arguments": {"path": "big.txt"}}]},
        {"text": "готово"},
    ]
    orch = Orchestrator(state, journal, ScriptedModel(script), cfg)
    assert orch.run() == "completed"
    first = state.calls["r1"].last_result
    assert first["ok"] and first["data"]["truncated"] is True
    assert "truncated" in first["data"]["content"]
    assert state.calls["r2"].refused["result"]["error"]["code"] == "BUDGET_EXCEEDED"
