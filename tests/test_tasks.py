"""Уровень 9: задачи с зависимостями, подтверждение результата runtime-ом,
незакрываемость финальным текстом, неперезапуск завершённых после resume."""

from __future__ import annotations

import pytest

from agent import recovery
from agent.events import Journal
from agent.llm import ScriptedModel
from agent.loop import FailpointCrash, Orchestrator
from agent.state import RunState

from conftest import (
    CHECKPOINT_SCRIPT,
    ORDER_FIXED,
    TASK,
    make_cfg,
    run_until_done,
    start_run,
)

PLAN_DEPS = {
    "goal": "Демонстрация зависимостей задач",
    "steps": [
        {
            "id": "a",
            "description": "Убедиться, что файлы проекта на месте",
            "tools": ["read_file"],
            "verification": {"command": "test -f order.py -a -f test_order.py"},
        },
        {
            "id": "b",
            "description": "Исправить order.py",
            "tools": ["write_file"],
            "depends_on": ["a"],
            "verification": {
                "command": "python -c \"from order import calculate_total; "
                           "assert calculate_total([100, 50], 10) == 135\""
            },
        },
        {
            "id": "c",
            "description": "Финальная проверка тестами",
            "tools": ["Bash"],
            "depends_on": ["b"],
            "verification": {"command": "python -m unittest -v"},
        },
    ],
    "done_criteria": "все проверки зелёные",
}


def test_dependencies_block_completion(workspace, runs_dir):
    """Задача не закрывается, пока не завершены её зависимости."""
    script = [
        {"tool_calls": [{"id": "k1", "name": "submit_plan",
                         "arguments": {"plan": PLAN_DEPS}}]},
        # попытка закрыть 'c' первой — зависимости a и b не завершены
        {"tool_calls": [{"id": "k2", "name": "complete_task",
                         "arguments": {"task_id": "c"}}]},
        {"tool_calls": [{"id": "k3", "name": "complete_task",
                         "arguments": {"task_id": "a"}}]},
        {"tool_calls": [{"id": "k4", "name": "write_file",
                         "arguments": {"path": "order.py", "content": ORDER_FIXED}}]},
        {"tool_calls": [{"id": "k5", "name": "complete_task",
                         "arguments": {"task_id": "b"}}]},
        {"tool_calls": [{"id": "k6", "name": "complete_task",
                         "arguments": {"task_id": "c"}}]},
        {"text": "готово"},
    ]
    cfg = make_cfg(workspace, runs_dir)
    state, journal = start_run(cfg, TASK, plan_mode=True)
    orch = Orchestrator(state, journal, ScriptedModel(script), cfg)

    assert run_until_done(orch) == "completed"

    early = state.calls["k2"].last_result
    assert early["error"]["code"] == "TASK_DEPENDENCIES_NOT_MET"
    # в сообщении — прямые зависимости задачи c (b); a — транзитивная через b
    assert "b" in early["error"]["message"]

    # порядок запуска: a → b → c (каждая после завершения зависимостей)
    loaded = Journal.load(journal.path)
    started_order = [e["task_id"] for e in loaded if e["event"] == "task_started"]
    assert started_order == ["a", "b", "c"]
    assert [state.tasks[tid].status for tid in ("a", "b", "c")] == ["completed"] * 3


def test_verification_by_runtime_not_by_model_words(workspace, runs_dir):
    """Проверка не пройдена — задача остаётся открытой; заявление модели
    ничего не закрывает, только exit code команды проверки."""
    script = [
        {"tool_calls": [{"id": "k1", "name": "submit_plan",
                         "arguments": {"plan": PLAN_DEPS}}]},
        {"tool_calls": [{"id": "k2", "name": "complete_task",
                         "arguments": {"task_id": "a"}}]},
        # order.py ещё не исправлен: проверка b обязана провалиться
        {"tool_calls": [{"id": "k3", "name": "complete_task",
                         "arguments": {"task_id": "b"}}]},
        {"tool_calls": [{"id": "k4", "name": "write_file",
                         "arguments": {"path": "order.py", "content": ORDER_FIXED}}]},
        {"tool_calls": [{"id": "k5", "name": "complete_task",
                         "arguments": {"task_id": "b"}}]},
        {"tool_calls": [{"id": "k6", "name": "complete_task",
                         "arguments": {"task_id": "c"}}]},
        {"text": "готово"},
    ]
    cfg = make_cfg(workspace, runs_dir)
    state, journal = start_run(cfg, TASK, plan_mode=True)
    orch = Orchestrator(state, journal, ScriptedModel(script), cfg)

    assert run_until_done(orch) == "completed"

    failed = state.calls["k3"].last_result
    assert failed["error"]["code"] == "VERIFICATION_FAILED"
    assert failed["error"]["data"]["verification"]["exit_code"] == 1
    # задача закрыта только успешной проверкой, и лишь однажды
    loaded = Journal.load(journal.path)
    b_completed = [e for e in loaded
                   if e["event"] == "task_completed" and e["task_id"] == "b"]
    assert len(b_completed) == 1
    assert b_completed[0]["verification"]["exit_code"] == 0


def test_final_text_does_not_close_open_tasks(workspace, runs_dir):
    """Финальный текст при открытых задачах не завершает запуск (уровень 9)."""
    script = [
        {"tool_calls": [{"id": "k1", "name": "submit_plan",
                         "arguments": {"plan": PLAN_DEPS}}]},
        {"text": "всё готово"},  # преждевременно
        {"tool_calls": [{"id": "k2", "name": "complete_task",
                         "arguments": {"task_id": "a"}}]},
        {"tool_calls": [{"id": "k3", "name": "write_file",
                         "arguments": {"path": "order.py", "content": ORDER_FIXED}}]},
        {"tool_calls": [{"id": "k4", "name": "complete_task",
                         "arguments": {"task_id": "b"}}]},
        {"tool_calls": [{"id": "k5", "name": "complete_task",
                         "arguments": {"task_id": "c"}}]},
        {"text": "готово"},
    ]
    cfg = make_cfg(workspace, runs_dir)
    state, journal = start_run(cfg, TASK, plan_mode=True)
    orch = Orchestrator(state, journal, ScriptedModel(script), cfg)

    assert run_until_done(orch) == "completed"
    # runtime вернул модели замечание о незакрытых задачах
    notes = [m["content"] for m in state.messages if m.get("role") == "user"]
    assert any("преждевременный" in text for text in notes)
    assert [t.status for t in state.tasks.values()] == ["completed"] * 3


def test_completed_tasks_not_restarted_after_resume(workspace, runs_dir):
    """После resume завершённые задачи не запускаются заново и не
    перепроверяются; продолжает только незавершённая."""
    cfg = make_cfg(workspace, runs_dir, failpoint="after_execute:c8")
    state, journal = start_run(cfg, TASK, plan_mode=True)
    orch = Orchestrator(state, journal, ScriptedModel(CHECKPOINT_SCRIPT[:7]), cfg)
    with pytest.raises(FailpointCrash):
        run_until_done(orch)

    cfg2 = make_cfg(workspace, runs_dir)
    loaded = Journal.load(journal.path)
    state2 = RunState.replay(loaded, state.run_id)
    assert state2.tasks["t1"].status == "completed"
    assert state2.tasks["t2"].status == "completed"
    assert state2.tasks["t3"].status == "in_progress"  # начата, не подтверждена

    orch2 = Orchestrator(state2, Journal(journal.path), ScriptedModel([]), cfg2)
    undefined, pending = recovery.analyze(state2)
    assert undefined == ["c8"] and pending == []
    recovery.apply_decision(orch2, "c8", "skip")

    resume_script = [
        # повторное подтверждение уже закрытой задачи — без перепроверки
        {"tool_calls": [{"id": "r1", "name": "complete_task",
                         "arguments": {"task_id": "t1"}}]},
        {"tool_calls": [{"id": "r2", "name": "complete_task",
                         "arguments": {"task_id": "t3"}}]},
        {"text": "готово"},
    ]
    orch2.model = ScriptedModel(resume_script)
    assert orch2.run() == "completed"

    again = orch2.state.calls["r1"].last_result
    assert again["ok"] and again["data"]["already_completed"] is True

    reloaded = Journal.load(journal.path)
    # каждая задача запускалась ровно один раз, t1 подтверждена один раз
    started = [e["task_id"] for e in reloaded if e["event"] == "task_started"]
    assert started == ["t1", "t2", "t3"]
    t1_completed = [e for e in reloaded
                    if e["event"] == "task_completed" and e["task_id"] == "t1"]
    assert len(t1_completed) == 1
    assert orch2.state.tasks["t3"].status == "completed"
