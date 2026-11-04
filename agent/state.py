"""Состояние запуска: replay журнала событий (event sourcing).

Текущее состояние — свёртка (fold) всех событий журнала, других источников
истины нет. Это даёт восстановление после сбоя в любой точке: replay
детерминированно восстанавливает историю сообщений, tool calls, бюджет
и решения пользователя.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from agent import events as ev
from agent.budget import Counters
from agent.config import Limits

TERMINAL_STATUSES = {"completed", "limit_reached", "aborted", "failed"}
ACTIVE_STATUSES = {"planning", "awaiting_approval", "executing"}


@dataclass
class Attempt:
    """Попытка исполнения tool call.

    Явный повтор неопределённого вызова (блок 2) — это attempt с номером+1
    того же tool_call_id, а не «тихий» перезапуск.
    """

    number: int
    status: str  # in_flight | completed | failed
    result: dict | None = None


@dataclass
class CallRecord:
    id: str
    name: str
    args_json: str  # канонический JSON аргументов (sort_keys) для сравнения
    side_effect: bool = False
    task_id: str | None = None  # уровень 9: связь вызова с текущей задачей
    attempts: list[Attempt] = field(default_factory=list)
    refused: dict | None = None  # последний отказ (вызов не исполнялся)

    @property
    def status(self) -> str:
        if self.attempts:
            return self.attempts[-1].status
        return "refused" if self.refused else "new"

    @property
    def last_result(self) -> dict | None:
        return self.attempts[-1].result if self.attempts else None


@dataclass
class TaskRecord:
    """Задача уровня 9: шаг плана под управлением runtime.

    Статус меняет только runtime по фактическим событиям:
    pending → in_progress → completed. Текст модели «задача готова»
    статуса не меняет — нужно подтверждение проверки (task_completed).
    """

    id: str
    description: str
    depends_on: list[str] = field(default_factory=list)
    verification: dict | None = None  # {"command": str, "expect_exit_code"?: int}
    status: str = "pending"
    verification_result: dict | None = None


@dataclass
class RunState:
    run_id: str
    task: str = ""
    mode: str = "direct"  # plan | direct
    phase: str = "executing"  # planning | executing
    status: str = "executing"
    workspace: str = ""
    limits: Limits = field(default_factory=Limits)
    messages: list[dict] = field(default_factory=list)
    calls: dict[str, CallRecord] = field(default_factory=dict)
    counters: Counters = field(default_factory=Counters)
    plan: dict | None = None
    decisions: list[dict] = field(default_factory=list)
    final_answer: str | None = None
    stop_reason: str | None = None
    turn: int = 0
    # Уровень 9
    tasks: dict[str, TaskRecord] = field(default_factory=dict)
    # Уровень 10
    snapshots: list[dict] = field(default_factory=list)
    changes: list[dict] = field(default_factory=list)
    last_tree: dict[str, str] | None = None  # relpath -> sha256
    rolled_back: bool = False

    @classmethod
    def replay(cls, events: list[dict], run_id: str) -> "RunState":
        state = cls(run_id=run_id)
        for event in events:
            state.apply(event)
        return state

    # ------------------------------------------------------------------ fold

    def apply(self, event: dict) -> None:
        kind = event["event"]
        handler = self._HANDLERS.get(kind)
        if handler is None:
            raise ValueError(f"неизвестное событие журнала: {kind!r}")
        handler(self, event)

    def _on_run_started(self, event: dict) -> None:
        self.task = event["task"]
        self.mode = event["mode"]
        self.workspace = event["workspace"]
        self.limits = Limits(**event["limits"])
        self.phase = "planning" if self.mode == "plan" else "executing"
        self.status = self.phase
        # История начинается с системного промпта и задачи пользователя.
        self.messages = list(event.get("initial_messages") or [
            {"role": "user", "content": event["initial_message"]}
        ])

    def _on_budget_debited(self, event: dict) -> None:
        if event.get("kind") == "model_request":
            self.counters.model_requests += 1

    def _on_model_response(self, event: dict) -> None:
        self.turn = event["turn"]
        self.messages.append(event["message"])

    def _on_tool_call_started(self, event: dict) -> None:
        record = self.calls.get(event["id"])
        if record is None:
            record = CallRecord(
                id=event["id"],
                name=event["name"],
                args_json=event["args_json"],
                side_effect=event.get("side_effect", False),
                task_id=event.get("task_id"),
            )
            self.calls[event["id"]] = record
        record.task_id = event.get("task_id")
        record.attempts.append(Attempt(number=event.get("attempt", 1), status="in_flight"))
        self.counters.tool_calls += 1
        if record.side_effect:
            self.counters.side_effects += 1

    def _on_tool_call_finished(self, event: dict) -> None:
        record = self.calls[event["id"]]
        attempt = record.attempts[-1]
        attempt.status = "completed" if event["ok"] else "failed"
        attempt.result = event["result"]
        self.counters.output_bytes += event.get("size", 0)
        self.messages.append(self._tool_message(event["id"], event["result"]))

    def _on_tool_call_refused(self, event: dict) -> None:
        record = self.calls.get(event["id"])
        if record is None:
            record = CallRecord(
                id=event["id"],
                name=event.get("name", "unknown"),
                args_json=event.get("args_json", "{}"),
            )
            self.calls[event["id"]] = record
        record.refused = event
        self.messages.append(self._tool_message(event["id"], event["result"]))

    def _on_tool_call_replayed(self, event: dict) -> None:
        # Повтор tool_call_id с теми же аргументами: сохранённый результат
        # возвращается модели, действие не исполняется повторно.
        self.messages.append(self._tool_message(event["id"], event["result"]))

    def _on_plan_saved(self, event: dict) -> None:
        self.plan = event["plan"]
        self.status = "awaiting_approval"

    def _on_plan_approved(self, event: dict) -> None:
        self.status = "executing"
        self.phase = "executing"
        self.messages.append({"role": "user", "content": event["text"]})

    def _on_plan_rejected(self, event: dict) -> None:
        self.status = "aborted"
        self.stop_reason = f"план отклонён: {event.get('reason', '')}".strip()

    def _on_decision_recorded(self, event: dict) -> None:
        self.decisions.append(event)

    def _on_user_note(self, event: dict) -> None:
        self.messages.append({"role": "user", "content": event["text"]})

    def _on_run_finished(self, event: dict) -> None:
        self.status = event["status"]
        self.stop_reason = event.get("reason")
        self.final_answer = event.get("final_answer")

    # Уровень 9: задачи.

    def _on_tasks_created(self, event: dict) -> None:
        for task in event["tasks"]:
            self.tasks[task["id"]] = TaskRecord(
                id=task["id"],
                description=task["description"],
                depends_on=list(task.get("depends_on", [])),
                verification=task.get("verification"),
            )

    def _on_task_started(self, event: dict) -> None:
        self.tasks[event["task_id"]].status = "in_progress"

    def _on_task_completed(self, event: dict) -> None:
        task = self.tasks[event["task_id"]]
        task.status = "completed"
        task.verification_result = event.get("verification")

    # Уровень 10: снапшоты и журнал изменений.

    def _on_snapshot_saved(self, event: dict) -> None:
        self.snapshots.append(
            {"snapshot_id": event["snapshot_id"], "file": event["file"]}
        )
        if self.last_tree is None:
            self.last_tree = event["tree"]

    def _on_change_recorded(self, event: dict) -> None:
        self.changes.append(event)
        self.last_tree = event["after_tree"]

    def _on_rollback_performed(self, event: dict) -> None:
        self.rolled_back = True

    @staticmethod
    def _tool_message(call_id: str, result: dict) -> dict:
        return {
            "role": "tool",
            "tool_call_id": call_id,
            "content": json.dumps(result, ensure_ascii=False),
        }

    _HANDLERS = {
        ev.RUN_STARTED: _on_run_started,
        ev.BUDGET_DEBITED: _on_budget_debited,
        ev.MODEL_RESPONSE: _on_model_response,
        ev.TOOL_CALL_STARTED: _on_tool_call_started,
        ev.TOOL_CALL_FINISHED: _on_tool_call_finished,
        ev.TOOL_CALL_REFUSED: _on_tool_call_refused,
        ev.TOOL_CALL_REPLAYED: _on_tool_call_replayed,
        ev.PLAN_SAVED: _on_plan_saved,
        ev.PLAN_APPROVED: _on_plan_approved,
        ev.PLAN_REJECTED: _on_plan_rejected,
        ev.DECISION_RECORDED: _on_decision_recorded,
        ev.USER_NOTE: _on_user_note,
        ev.RUN_FINISHED: _on_run_finished,
        ev.TASKS_CREATED: _on_tasks_created,
        ev.TASK_STARTED: _on_task_started,
        ev.TASK_COMPLETED: _on_task_completed,
        ev.SNAPSHOT_SAVED: _on_snapshot_saved,
        ev.CHANGE_RECORDED: _on_change_recorded,
        ev.ROLLBACK_PERFORMED: _on_rollback_performed,
    }

    # ------------------------------------------------------------- запросы

    def undefined_calls(self) -> list[CallRecord]:
        """Начатые, но не завершённые вызовы — кандидаты на решение пользователя."""
        return [rec for rec in self.calls.values() if rec.status == "in_flight"]

    # Уровень 9: текущая задача (последовательное исполнение).

    @property
    def current_task_id(self) -> str | None:
        for task_id, task in self.tasks.items():  # порядок создания = порядок плана
            if task.status == "in_progress":
                return task_id
        return None

    def open_tasks(self) -> list[TaskRecord]:
        return [t for t in self.tasks.values() if t.status != "completed"]

    def next_eligible_task(self) -> TaskRecord | None:
        """Первая pending-задача, все зависимости которой завершены."""
        for task in self.tasks.values():
            if task.status != "pending":
                continue
            if all(self.tasks[d].status == "completed" for d in task.depends_on):
                return task
        return None

    def missing_tool_answers(self) -> list[str]:
        """tool_call_id из assistant-сообщений, для которых нет role=tool ответа."""
        asked: list[str] = []
        answered = {
            m["tool_call_id"] for m in self.messages if m.get("role") == "tool"
        }
        for message in self.messages:
            if message.get("role") != "assistant":
                continue
            for call in message.get("tool_calls") or []:
                call_id = call.get("id")
                if call_id not in answered and call_id not in asked:
                    asked.append(call_id)
        return asked

    def call_name(self, call_id: str) -> str:
        record = self.calls.get(call_id)
        if record:
            return record.name
        for message in self.messages:
            for call in message.get("tool_calls") or []:
                if call.get("id") == call_id:
                    return call.get("function", {}).get("name", "unknown")
        return "unknown"
