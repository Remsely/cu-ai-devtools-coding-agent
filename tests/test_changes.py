"""Уровень 10: снапшоты дерева, журнал изменений, diff, откат, конфликты.

Идентификаторы простого сценария: m1 write_file(order.py), m2 Bash(notes.txt).
"""

from __future__ import annotations

import pytest

from agent import changes, recovery
from agent.events import Journal
from agent.llm import ScriptedModel
from agent.loop import FailpointCrash, Orchestrator
from agent.state import RunState

from conftest import (
    ORDER_BUGGY,
    ORDER_FIXED,
    ORDER_TEST,
    TASK,
    make_cfg,
    start_run,
)

SIMPLE_SCRIPT = [
    {"tool_calls": [{"id": "m1", "name": "write_file",
                     "arguments": {"path": "order.py", "content": ORDER_FIXED}}]},
    {"tool_calls": [{"id": "m2", "name": "Bash",
                     "arguments": {"command": "echo hi > notes.txt"}}]},
    {"text": "готово"},
]


def _run(script, workspace, runs_dir, **kwargs):
    cfg = make_cfg(workspace, runs_dir, **kwargs)
    state, journal = start_run(cfg, TASK, plan_mode=False)
    orch = Orchestrator(state, journal, ScriptedModel(script), cfg)
    status = orch.run()
    return cfg, state, journal, status


def test_diff_view_and_rollback(workspace, runs_dir):
    """Изменение order.py, создание notes.txt, просмотр diff, откат."""
    cfg, state, journal, status = _run(SIMPLE_SCRIPT, workspace, runs_dir)
    assert status == "completed"

    # журнал изменений: исходное/итоговое состояния и хеши
    aggregated = changes.aggregate_changes(state)
    assert aggregated["order.py"]["change"] == "modified"
    assert aggregated["order.py"]["before_hash"] == changes._hash_bytes(
        ORDER_BUGGY.encode("utf-8")
    )
    assert aggregated["order.py"]["after_hash"] == changes._hash_bytes(
        ORDER_FIXED.encode("utf-8")
    )
    assert aggregated["notes.txt"]["change"] == "created"
    assert aggregated["notes.txt"]["before_hash"] is None

    # diff-отчёт по данным журнала (работает и после перезапуска)
    replayed = RunState.replay(Journal.load(journal.path), state.run_id)
    report = "\n".join(changes.diff_report(replayed, cfg))
    assert "order.py: modified" in report
    assert "notes.txt: created" in report
    assert "---" in report and "+++" in report  # unified diff присутствует

    # откат: восстанавливает только файлы этого запуска
    result = changes.rollback(replayed, Journal(journal.path), cfg)
    assert result["restored"] == ["order.py"]
    assert result["removed"] == ["notes.txt"]
    assert (workspace / "order.py").read_text(encoding="utf-8") == ORDER_BUGGY
    assert not (workspace / "notes.txt").exists()
    assert (workspace / "test_order.py").read_text(encoding="utf-8") == ORDER_TEST

    final = RunState.replay(Journal.load(journal.path), state.run_id)
    assert final.rolled_back is True


def test_rollback_conflict_on_external_edit(workspace, runs_dir):
    """Посторонняя правка после агента — конфликт, не перезаписывается ничего."""
    cfg, state, journal, status = _run(SIMPLE_SCRIPT, workspace, runs_dir)
    assert status == "completed"

    # внешняя правка файла, изменённого запуском
    with open(workspace / "order.py", "a", encoding="utf-8") as fh:
        fh.write("# внешняя правка\n")

    replayed = RunState.replay(Journal.load(journal.path), state.run_id)
    with pytest.raises(changes.ChangeConflict) as exc_info:
        changes.rollback(replayed, Journal(journal.path), cfg)
    assert exc_info.value.paths == ["order.py"]
    assert "извне" in str(exc_info.value)

    # ничего не перезаписано: внешняя правка на месте, notes.txt не тронут
    assert "# внешняя правка" in (workspace / "order.py").read_text(encoding="utf-8")
    assert (workspace / "notes.txt").exists()


def test_tree_conflict_blocks_next_side_effect(workspace, runs_dir):
    """Расхождение дерева с последним снимком останавливает побочное действие."""
    from pathlib import Path

    class ExternalEditorModel(ScriptedModel):
        """Между репликами имитирует посторонний процесс, правящий файл."""

        def __init__(self, script, edit_before_reply: int, target: Path, text: str):
            super().__init__(script)
            self.edit_before_reply = edit_before_reply
            self.target = target
            self.text = text

        def chat(self, messages, tools):
            if self._pos + 1 == self.edit_before_reply:
                with open(self.target, "a", encoding="utf-8") as fh:
                    fh.write(self.text)
            return super().chat(messages, tools)

    script = [
        {"tool_calls": [{"id": "m1", "name": "write_file",
                         "arguments": {"path": "order.py", "content": ORDER_FIXED}}]},
        {"tool_calls": [{"id": "m2", "name": "write_file",
                         "arguments": {"path": "order.py", "content": "v2"}}]},
        {"text": "готово"},
    ]
    cfg = make_cfg(workspace, runs_dir)
    state, journal = start_run(cfg, TASK, plan_mode=False)
    model = ExternalEditorModel(script, 2, workspace / "order.py", "# извне\n")
    orch = Orchestrator(state, journal, model, cfg)
    assert orch.run() == "completed"

    refused = state.calls["m2"].refused["result"]
    assert refused["error"]["code"] == "TREE_MODIFIED_EXTERNALLY"
    assert "order.py" in refused["error"]["message"]
    # файл не перезаписан отказанным действием: правка извне на месте
    content = (workspace / "order.py").read_text(encoding="utf-8")
    assert content == ORDER_FIXED + "# извне\n"


def test_undefined_effect_attributed_then_rollback(workspace, runs_dir):
    """Сбой на write → откат до разрешения невозможен → skip относит эффект
    к запуску → откат восстанавливает исходный файл."""
    cfg = make_cfg(workspace, runs_dir, failpoint="after_execute:m1")
    state, journal = start_run(cfg, TASK, plan_mode=False)
    orch = Orchestrator(state, journal, ScriptedModel(SIMPLE_SCRIPT[:1]), cfg)
    with pytest.raises(FailpointCrash):
        orch.run()
    assert (workspace / "order.py").read_text(encoding="utf-8") == ORDER_FIXED

    cfg2 = make_cfg(workspace, runs_dir)
    state2 = RunState.replay(Journal.load(journal.path), state.run_id)

    # до разрешения неопределённого вызова откат не выполняется (блок 2 + 10)
    with pytest.raises(changes.ChangeConflict) as blocked:
        changes.rollback(state2, Journal(journal.path), cfg2)
    assert "resume" in str(blocked.value)

    orch2 = Orchestrator(state2, Journal(journal.path), ScriptedModel([]), cfg2)
    recovery.apply_decision(orch2, "m1", "skip")

    # эффект неопределённого вызова отнесён к запуску решением пользователя
    attributed = [
        e for e in Journal.load(journal.path)
        if e["event"] == "change_recorded" and e.get("attribution") == "undefined_call"
    ]
    assert attributed and attributed[0]["diff"][0]["path"] == "order.py"

    final = RunState.replay(Journal.load(journal.path), state.run_id)
    result = changes.rollback(final, Journal(journal.path), cfg2)
    assert result["restored"] == ["order.py"]
    assert (workspace / "order.py").read_text(encoding="utf-8") == ORDER_BUGGY


def test_initial_snapshot_and_no_change_runs(workspace, runs_dir):
    """Чтения не создают diff; исходный снимок фиксирует дерево до действий."""
    script = [
        {"tool_calls": [{"id": "r1", "name": "read_file",
                         "arguments": {"path": "order.py"}}]},
        {"text": "готово"},
    ]
    cfg, state, journal, status = _run(script, workspace, runs_dir)
    assert status == "completed"
    assert state.snapshots, "исходный снимок сохранён в начале исполнения"
    assert set(state.last_tree) == {"order.py", "test_order.py"}
    assert changes.aggregate_changes(state) == {}
    result = changes.rollback(state, Journal(journal.path), cfg)
    assert result["restored"] == [] and result["removed"] == []
