"""Тесты журнала и восстановления состояния replay-ем."""

from __future__ import annotations

from agent.config import Limits
from agent.events import Journal
from agent.state import RunState


def _base_events() -> list[dict]:
    return [
        {
            "event": "run_started",
            "task": "t",
            "mode": "direct",
            "workspace": "/w",
            "limits": Limits().as_dict(),
            "initial_message": "Задача: t",
        },
        {"event": "budget_debited", "kind": "model_request"},
        {
            "event": "model_response",
            "turn": 1,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ],
            },
        },
        {
            "event": "tool_call_started",
            "id": "c1",
            "name": "read_file",
            "args_json": "{}",
            "side_effect": False,
            "attempt": 1,
        },
    ]


def test_journal_roundtrip_and_append_after_restart(tmp_path):
    journal = Journal(tmp_path / "j.jsonl")
    journal.append("run_started", task="t")
    journal.append("user_note", text="hi")
    loaded = Journal.load(journal.path)
    assert [e["event"] for e in loaded] == ["run_started", "user_note"]
    assert loaded[0]["seq"] == 1 and "ts" in loaded[0]

    # «перезапуск»: новый Journal дозаписывает с корректным seq
    journal2 = Journal(journal.path)
    journal2.append("user_note", text="again")
    loaded = Journal.load(journal.path)
    assert loaded[-1]["seq"] == 3


def test_replay_detects_undefined_call():
    state = RunState.replay(_base_events(), "r1")
    assert state.counters.model_requests == 1
    assert state.counters.tool_calls == 1
    assert state.counters.side_effects == 0
    assert [c.id for c in state.undefined_calls()] == ["c1"]
    assert state.missing_tool_answers() == ["c1"]

    state.apply(
        {"event": "tool_call_finished", "id": "c1", "attempt": 1, "ok": True,
         "size": 10, "result": {"ok": True, "data": {}}}
    )
    assert state.undefined_calls() == []
    assert state.missing_tool_answers() == []
    assert state.counters.output_bytes == 10
    # ответ tool пришёл с исходным tool_call_id
    tool_msg = [m for m in state.messages if m["role"] == "tool"][-1]
    assert tool_msg["tool_call_id"] == "c1"


def test_replay_counts_side_effects():
    events = _base_events()
    events.append(
        {"event": "tool_call_started", "id": "c2", "name": "Bash",
         "args_json": '{"command":"ls"}', "side_effect": True, "attempt": 1}
    )
    state = RunState.replay(events, "r1")
    assert state.counters.tool_calls == 2
    assert state.counters.side_effects == 1


def test_budget_counters_survive_replay_identically():
    events = _base_events()
    state1 = RunState.replay(events, "r1")
    state2 = RunState.replay(events, "r1")
    assert state1.counters == state2.counters  # resume не обнуляет бюджет
