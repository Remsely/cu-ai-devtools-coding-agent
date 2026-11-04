"""Восстановление после сбоя (блок 2): неопределённые вызовы, решения,
идемпотентность, не начатые вызовы пачки, защита протокола.

Идентификаторы сценария: c1 list, c2/c3 reads, c4 submit_plan,
c5 complete_task(t1), c6 write_file, c7 complete_task(t2),
c8 Bash(тесты), c9 complete_task(t3), текст.
Краш устраиваем на c6 (write_file): между выполнением и сохранением результата.
"""

from __future__ import annotations

import pytest

from agent import recovery
from agent.events import Journal
from agent.llm import ScriptedModel
from agent.loop import FailpointCrash, Orchestrator
from agent.state import RunState

from conftest import (
    CHECKPOINT_SCRIPT,
    ORDER_BUGGY,
    ORDER_FIXED,
    TASK,
    make_cfg,
    run_until_done,
    start_run,
)


def _resume_orch(state, journal_path, cfg, script):
    state2 = RunState.replay(Journal.load(journal_path), state.run_id)
    return Orchestrator(state2, Journal(journal_path), ScriptedModel(script), cfg)


def test_crash_after_execute_then_skip(workspace, runs_dir):
    """Сбой между выполнением write_file и сохранением результата."""
    cfg = make_cfg(workspace, runs_dir, failpoint="after_execute:c6")
    state, journal = start_run(cfg, TASK, plan_mode=True)
    orch = Orchestrator(state, journal, ScriptedModel(CHECKPOINT_SCRIPT[:5]), cfg)

    with pytest.raises(FailpointCrash):
        run_until_done(orch)

    # действие выполнилось, результата в журнале нет
    assert (workspace / "order.py").read_text(encoding="utf-8") == ORDER_FIXED
    loaded = Journal.load(journal.path)
    assert any(e["event"] == "tool_call_started" and e["id"] == "c6" for e in loaded)
    assert not any(e["event"] == "tool_call_finished" and e["id"] == "c6" for e in loaded)
    assert not any(e["event"] == "run_finished" for e in loaded)

    # resume: replay находит неопределённый вызов, пользователь выбирает skip
    cfg2 = make_cfg(workspace, runs_dir)
    orch2 = _resume_orch(state, journal.path, cfg2, CHECKPOINT_SCRIPT[5:])
    undefined, pending = recovery.analyze(orch2.state)
    assert undefined == ["c6"] and pending == []

    recovery.apply_decision(orch2, "c6", "skip")
    assert run_until_done(orch2) == "completed"

    # повторного исполнения не было; шаг помечен неуспешным
    assert len(orch2.state.calls["c6"].attempts) == 1
    assert orch2.state.calls["c6"].attempts[-1].status == "failed"
    skip_result = orch2.state.calls["c6"].last_result
    assert skip_result["error"]["code"] == "UNDEFINED_SKIPPED"

    # бюджет не обнулился: 5 запросов до сбоя + 4 после (c7, c8, c9, текст)
    assert orch2.state.counters.model_requests == 9
    assert orch2.state.counters.tool_calls == 9  # c1..c5 + c6(начат) + c7..c9
    # решения пользователя сохранены с хешем аргументов
    decision = orch2.state.decisions[0]
    assert decision["choice"] == "skip" and decision["call_id"] == "c6"
    assert decision["args_hash"].startswith("sha256:")
    # задачи пережили сбой: t1/t2 завершены до сбоя, t3 — после
    assert orch2.state.tasks["t1"].status == "completed"
    assert orch2.state.tasks["t3"].status == "completed"
    assert orch2.state.calls["c8"].last_result["data"]["exit_code"] == 0


def test_crash_after_execute_retry_is_idempotent(workspace, runs_dir):
    """Явный retry — новая попытка; write_file не меняет файл повторно."""
    cfg = make_cfg(workspace, runs_dir, failpoint="after_execute:c6")
    state, journal = start_run(cfg, TASK, plan_mode=True)
    orch = Orchestrator(state, journal, ScriptedModel(CHECKPOINT_SCRIPT[:5]), cfg)
    with pytest.raises(FailpointCrash):
        run_until_done(orch)
    assert (workspace / "order.py").read_text(encoding="utf-8") == ORDER_FIXED

    cfg2 = make_cfg(workspace, runs_dir)
    orch2 = _resume_orch(state, journal.path, cfg2, CHECKPOINT_SCRIPT[5:])
    recovery.apply_decision(orch2, "c6", "retry")

    # вторая попытка завершилась, файл не перезаписывался (already_applied)
    assert len(orch2.state.calls["c6"].attempts) == 2
    assert orch2.state.calls["c6"].attempts[-1].status == "completed"
    assert orch2.state.calls["c6"].last_result["data"]["already_applied"] is True
    assert orch2.state.calls["c6"].last_result["data"]["bytes_written"] == 0
    # списание за вторую попытку: c1..c5 + первая попытка c6 + retry
    assert orch2.state.counters.tool_calls == 7

    assert run_until_done(orch2) == "completed"
    assert (workspace / "order.py").read_text(encoding="utf-8") == ORDER_FIXED


def test_crash_after_start_marks_pending_batch_calls(workspace, runs_dir):
    """Сбой до исполнения: файл не менялся, не начатый вызов пачки помечается."""
    script = [
        {
            "tool_calls": [
                {"id": "x1", "name": "write_file",
                 "arguments": {"path": "order.py", "content": ORDER_FIXED}},
                {"id": "x2", "name": "read_file", "arguments": {"path": "order.py"}},
            ]
        },
        {"text": "готово"},
    ]
    cfg = make_cfg(workspace, runs_dir, failpoint="after_start:x1")
    state, journal = start_run(cfg, TASK, plan_mode=False)
    orch = Orchestrator(state, journal, ScriptedModel(script[:1]), cfg)
    with pytest.raises(FailpointCrash):
        orch.run()
    assert (workspace / "order.py").read_text(encoding="utf-8") == ORDER_BUGGY

    cfg2 = make_cfg(workspace, runs_dir)
    orch2 = _resume_orch(state, journal.path, cfg2, script[1:])
    undefined, pending = recovery.analyze(orch2.state)
    assert undefined == ["x1"] and pending == ["x2"]

    recovery.mark_not_executed(orch2, pending)
    recovery.apply_decision(orch2, "x1", "skip")
    assert orch2.run() == "completed"

    assert orch2.state.calls["x2"].refused["result"]["error"]["code"] == (
        "NOT_EXECUTED_AFTER_CRASH"
    )
    assert (workspace / "order.py").read_text(encoding="utf-8") == ORDER_BUGGY
    assert orch2.state.missing_tool_answers() == []


def test_abort_decision_terminates(workspace, runs_dir):
    cfg = make_cfg(workspace, runs_dir, failpoint="after_execute:c6")
    state, journal = start_run(cfg, TASK, plan_mode=True)
    orch = Orchestrator(state, journal, ScriptedModel(CHECKPOINT_SCRIPT[:5]), cfg)
    with pytest.raises(FailpointCrash):
        run_until_done(orch)

    cfg2 = make_cfg(workspace, runs_dir)
    orch2 = _resume_orch(state, journal.path, cfg2, [])
    recovery.apply_decision(orch2, "c6", "abort")
    assert orch2.state.status == "aborted"
    assert "c6" in orch2.state.stop_reason


def test_duplicate_tool_call_id_replay_and_protocol_error(workspace, runs_dir):
    """Повтор tool_call_id: те же аргументы — сохранённый результат без
    повторного изменения файлов; другие аргументы — ошибка протокола."""
    script = [
        {"tool_calls": [{"id": "d1", "name": "write_file",
                         "arguments": {"path": "order.py", "content": ORDER_FIXED}}]},
        {"tool_calls": [{"id": "d1", "name": "write_file",
                         "arguments": {"path": "order.py", "content": ORDER_FIXED}}]},
        {"tool_calls": [{"id": "d1", "name": "write_file",
                         "arguments": {"path": "order.py", "content": "other"}}]},
        {"text": "готово"},
    ]
    cfg = make_cfg(workspace, runs_dir)
    state, journal = start_run(cfg, TASK, plan_mode=False)
    orch = Orchestrator(state, journal, ScriptedModel(script), cfg)
    assert orch.run() == "completed"

    assert state.counters.tool_calls == 1  # исполнение было ровно одно
    assert len(state.calls["d1"].attempts) == 1
    loaded = Journal.load(journal.path)
    assert any(e["event"] == "tool_call_replayed" and e["id"] == "d1" for e in loaded)
    protocol_refusals = [
        e for e in loaded
        if e["event"] == "tool_call_refused"
        and e["result"]["error"]["code"] == "PROTOCOL_ERROR"
    ]
    assert len(protocol_refusals) == 1
    assert (workspace / "order.py").read_text(encoding="utf-8") == ORDER_FIXED
