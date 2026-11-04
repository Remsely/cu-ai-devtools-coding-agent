"""Журнал событий запуска — append-only WAL.

Каждое событие — одна строка JSON в runs/<run_id>/journal.jsonl.
Запись атомарна на уровне строки: flush + fsync сразу после append.
Текущее состояние запуска всегда можно получить replay-ем журнала,
поэтому других "источников истины" нет.

Ключевой паттерн блока 2 (persistence-before-execute):
перед действием с побочным эффектом пишется tool_call_started,
после выполнения — tool_call_finished. Падение между ними
даёт «неопределённый» вызов, который молча не повторяется.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Имена событий (стабильные, хранятся в журнале).
RUN_STARTED = "run_started"
BUDGET_DEBITED = "budget_debited"
MODEL_RESPONSE = "model_response"
TOOL_CALL_STARTED = "tool_call_started"
TOOL_CALL_FINISHED = "tool_call_finished"
TOOL_CALL_REFUSED = "tool_call_refused"
TOOL_CALL_REPLAYED = "tool_call_replayed"
PLAN_SAVED = "plan_saved"
PLAN_APPROVED = "plan_approved"
PLAN_REJECTED = "plan_rejected"
DECISION_RECORDED = "decision_recorded"
USER_NOTE = "user_note"
RUN_FINISHED = "run_finished"

# Уровень 9: задачи с зависимостями.
TASKS_CREATED = "tasks_created"
TASK_STARTED = "task_started"
TASK_COMPLETED = "task_completed"

# Уровень 10: снапшоты дерева и журнал изменений.
SNAPSHOT_SAVED = "snapshot_saved"
CHANGE_RECORDED = "change_recorded"
ROLLBACK_PERFORMED = "rollback_performed"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_run_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"r{stamp}-{uuid.uuid4().hex[:4]}"


class Journal:
    """Дозапись событий в журнал запуска с fsync после каждой строки.

    on_append — подписчик (обычно RunState.apply): каждое записанное событие
    немедленно сворачивается в живое состояние, поэтому runtime всегда
    работает с состоянием, равным replay журнала.
    """

    def __init__(self, path: Path, on_append=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existing = self.load(self.path) if self.path.exists() else []
        self._seq = len(existing)
        self.on_append = on_append
        self._fh = open(self.path, "a", encoding="utf-8")

    def append(self, event: str, **payload) -> dict:
        self._seq += 1
        record = {"seq": self._seq, "ts": utc_now(), "event": event, **payload}
        self._fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())
        if self.on_append is not None:
            self.on_append(record)
        return record

    def close(self) -> None:
        self._fh.close()

    @staticmethod
    def load(path: Path) -> list[dict]:
        events: list[dict] = []
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        return events
