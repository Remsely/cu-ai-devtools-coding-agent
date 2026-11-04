"""Контрольная задача (блок 1 + уровень 9) и Plan Mode (блок 3), офлайн.

ScriptedModel играет роль модели: реплики детерминированы, поэтому тест
доказывает свойства runtime, а не везение модели.
"""

from __future__ import annotations

from agent.events import Journal
from agent.llm import ScriptedModel
from agent.loop import Orchestrator
from agent.state import RunState

from conftest import (
    CHECKPOINT_SCRIPT,
    ORDER_BUGGY,
    ORDER_FIXED,
    ORDER_TEST,
    PLAN,
    TASK,
    make_cfg,
    run_until_done,
    start_run,
)


def test_checkpoint_task_end_to_end(workspace, runs_dir, capsys):
    """Полный сценарий: план → утверждение → задачи → исправление → тесты."""
    cfg = make_cfg(workspace, runs_dir)
    state, journal = start_run(cfg, TASK, plan_mode=True)
    orch = Orchestrator(state, journal, ScriptedModel(CHECKPOINT_SCRIPT), cfg)

    assert run_until_done(orch) == "completed"

    # агент действительно запускал тесты через Bash, и они зелёные
    bash_result = state.calls["c8"].last_result
    assert bash_result["ok"]
    assert bash_result["data"]["exit_code"] == 0
    # unittest пишет отчёт в stderr: test_discount/test_invalid_discount + OK
    report = bash_result["data"]["stdout"] + bash_result["data"]["stderr"]
    assert "test_discount" in report and "OK" in report

    # order.py исправлен, test_order.py не тронут (правило + наблюдение)
    assert (workspace / "order.py").read_text(encoding="utf-8") == ORDER_FIXED
    assert (workspace / "test_order.py").read_text(encoding="utf-8") == ORDER_TEST

    # несколько tool calls в одном ответе модели поддерживаются
    assistant_msgs = [m for m in state.messages if m["role"] == "assistant"]
    assert any(len(m.get("tool_calls") or []) == 2 for m in assistant_msgs)

    # каждый результат вернулся модели с исходным tool_call_id
    tool_msgs = [m for m in state.messages if m["role"] == "tool"]
    assert {m["tool_call_id"] for m in tool_msgs} == {f"c{i}" for i in range(1, 10)}

    # бюджет списан ровно по факту: 9 запросов к модели, 9 tool calls,
    # 2 действия с побочным эффектом (write_file + Bash); служебные
    # проверки задач бюджет не расходуют
    assert state.counters.model_requests == 9
    assert state.counters.tool_calls == 9
    assert state.counters.side_effects == 2

    # журнал: каждый started закрыт finished, run_finished есть
    loaded = Journal.load(journal.path)
    started = [e["id"] for e in loaded if e["event"] == "tool_call_started"]
    finished = [e["id"] for e in loaded if e["event"] == "tool_call_finished"]
    assert started == finished == [f"c{i}" for i in range(1, 10)]
    assert loaded[-1]["event"] == "run_finished" and loaded[-1]["status"] == "completed"

    # Уровень 9: все задачи завершены, подтверждены проверками runtime
    assert [t.status for t in state.tasks.values()] == ["completed"] * 3
    assert state.tasks["t3"].verification_result["exit_code"] == 0
    assert state.tasks["t2"].verification_result["ok"] is True
    # каждый вызов исполнения привязан к текущей задаче; в планировании task_id нет
    assert state.calls["c1"].task_id is None
    assert state.calls["c4"].task_id is None
    assert state.calls["c5"].task_id == "t1"
    assert state.calls["c6"].task_id == "t2"
    assert state.calls["c7"].task_id == "t2"
    assert state.calls["c8"].task_id == "t3"
    assert state.calls["c9"].task_id == "t3"

    # чеклист задач печатается в stdout
    out = capsys.readouterr().out
    assert "[чеклист]" in out and "[x] t1" in out and "[x] t3" in out

    # состояние полностью восстанавливается replay-ем
    replayed = RunState.replay(loaded, state.run_id)
    assert replayed.counters == state.counters
    assert replayed.status == "completed"
    assert replayed.final_answer == state.final_answer
    assert [t.status for t in replayed.tasks.values()] == ["completed"] * 3


def test_plan_mode_blocks_all_side_effects(workspace, runs_dir):
    """До утверждения плана — ни одного побочного эффекта (блок 3)."""
    script = [
        {"tool_calls": [{"id": "p1", "name": "write_file",
                         "arguments": {"path": "order.py", "content": "hacked"}}]},
        {"tool_calls": [{"id": "p2", "name": "Bash", "arguments": {"command": "echo hi"}}]},
        {"tool_calls": [{"id": "p3", "name": "list_files", "arguments": {}}]},
        {"tool_calls": [{"id": "p4", "name": "submit_plan", "arguments": {"plan": PLAN}}]},
    ]
    cfg = make_cfg(workspace, runs_dir)
    state, journal = start_run(cfg, TASK, plan_mode=True)
    orch = Orchestrator(state, journal, ScriptedModel(script), cfg)

    assert orch.run() == "awaiting_approval"

    for call_id in ("p1", "p2"):
        refused = state.calls[call_id].refused["result"]
        assert refused["error"]["code"] == "DENIED_BY_POLICY"
    assert state.calls["p3"].status == "completed"

    loaded = Journal.load(journal.path)
    side_started = [
        e for e in loaded
        if e["event"] == "tool_call_started" and e.get("side_effect")
    ]
    assert side_started == []  # ни одного побочного действия до утверждения
    assert (workspace / "order.py").read_text(encoding="utf-8") == ORDER_BUGGY
    assert state.plan is not None and state.plan["goal"] == PLAN["goal"]
    assert state.status == "awaiting_approval"


def test_invalid_plan_is_rejected_and_loop_continues(workspace, runs_dir):
    """Невалидный план — ошибка инструмента, цикл не падает, модель правит план."""
    script = [
        {"tool_calls": [{"id": "s1", "name": "submit_plan",
                         "arguments": {"plan": {"goal": "", "steps": []}}}]},
        {"tool_calls": [{"id": "s2", "name": "submit_plan", "arguments": {"plan": PLAN}}]},
    ]
    cfg = make_cfg(workspace, runs_dir)
    state, journal = start_run(cfg, TASK, plan_mode=True)
    orch = Orchestrator(state, journal, ScriptedModel(script), cfg)

    assert orch.run() == "awaiting_approval"
    first = state.calls["s1"].last_result
    assert not first["ok"]
    assert first["error"]["code"] == "INVALID_ARGUMENTS"
    assert "goal" in first["error"]["message"]
    assert state.calls["s2"].status == "completed"
    assert state.plan["goal"] == PLAN["goal"]


def test_stall_guard_stops_run(workspace, runs_dir):
    """Три текст-ответа подряд без инструментов — остановка, а не выжигание
    бюджета повторяющимися отговорками."""
    script = [{"text": t} for t in ("сейчас составлю план", "почти готово", "ещё минуту")]
    cfg = make_cfg(workspace, runs_dir)
    state, journal = start_run(cfg, TASK, plan_mode=True)
    orch = Orchestrator(state, journal, ScriptedModel(script), cfg)
    assert orch.run() == "failed"
    assert "трижды" in (state.stop_reason or "")
    assert state.counters.model_requests == 3  # бюджет не выжжен до лимита


def test_plan_passed_as_string_is_autoparsed(workspace, runs_dir):
    """Модель передала план JSON-строкой — runtime парсит, а не отклоняет."""
    import json as json_mod

    script = [
        {"tool_calls": [{"id": "s1", "name": "submit_plan",
                         "arguments": {"plan": json_mod.dumps(PLAN, ensure_ascii=False)}}]},
    ]
    cfg = make_cfg(workspace, runs_dir)
    state, journal = start_run(cfg, TASK, plan_mode=True)
    orch = Orchestrator(state, journal, ScriptedModel(script), cfg)
    assert orch.run() == "awaiting_approval"
    assert state.plan is not None and state.plan["goal"] == PLAN["goal"]


def test_rejection_terminates_run(workspace, runs_dir):
    cfg = make_cfg(workspace, runs_dir)
    state, journal = start_run(cfg, TASK, plan_mode=True)
    script = [{"tool_calls": [{"id": "p1", "name": "submit_plan",
                               "arguments": {"plan": PLAN}}]}]
    orch = Orchestrator(state, journal, ScriptedModel(script), cfg)
    assert orch.run() == "awaiting_approval"
    orch.reject_plan("тест")
    assert state.status == "aborted"
    assert orch.run() == "aborted"
