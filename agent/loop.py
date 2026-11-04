"""Agent loop: цикл «модель → инструменты → наблюдение» и политики runtime.

Обязанности (блок 1):
- модель сама решает, какой инструмент вызвать, нужен ли следующий шаг
  и когда вернуть финальный ответ;
- поддержка нескольких tool calls в одном ответе модели;
- проверка имени и аргументов ДО выполнения (слои: разбор → схема →
  реестр → политика → бюджет), результат — сообщением role=tool
  с исходным tool_call_id;
- ошибка инструмента не роняет процесс: модель получает конверт с кодом
  ошибки и выбирает другой шаг.

Бюджет (уровень 8): списание ДО действия (WAL), проверка пачки целиком,
остаток вывода передаётся инструменту и обрезается с пометкой truncated.
"""

from __future__ import annotations

import json

from agent import changes, events, tasks
from agent.budget import batch_refusal_reason, model_request_allowed, output_cap
from agent.config import Config
from agent.events import Journal
from agent.llm import (
    LLMAPIError,
    ScriptExhausted,
    ToolCallRequest,
    assistant_message,
)
from agent.planning import PLAN_TOOL, plan_summary_lines, validate_plan
from agent.state import TERMINAL_STATUSES, RunState
from agent.tools import EXECUTING_TOOLS, PLANNING_TOOLS, make_tools, tools_for_phase
from agent.tools.base import (
    BUDGET_EXCEEDED,
    DENIED_BY_POLICY,
    INVALID_ARGUMENTS,
    PARSE_ERROR,
    PROTOCOL_ERROR,
    TREE_MODIFIED_EXTERNALLY,
    UNKNOWN_TOOL,
    Tool,
    ToolContext,
    execute,
)
from agent.tools.task_tool import COMPLETE_TASK_TOOL

FAILPOINTS = ("after_start", "after_execute")


class FailpointCrash(RuntimeError):
    """Управляемый сбой для тестов: процесс останавливается в заданной точке.

    AGENT_FAILPOINT=after_start[:call_id]     — после записи tool_call_started,
                                                до исполнения;
    AGENT_FAILPOINT=after_execute[:call_id]   — после исполнения, ДО записи
                                                результата (имитация сбоя
                                                между действием и сохранением).
    """


def say(text: str) -> None:
    print(text, flush=True)


def _fmt_call(call: ToolCallRequest) -> str:
    """Компактное человекочитаемое описание вызова инструмента."""
    args = call.arguments or {}
    if call.name == "Bash":
        return f"Bash({str(args.get('command', ''))[:70]})"
    if call.name in ("read_file", "write_file"):
        return f"{call.name}({args.get('path', '')})"
    if call.name == "list_files":
        return "list_files()"
    if call.name == "complete_task":
        return f"complete_task({args.get('task_id', '')})"
    return call.name


def _args_json(call: ToolCallRequest) -> str:
    return json.dumps(call.arguments or {}, ensure_ascii=False, sort_keys=True)


def _cap_result(result: dict, cap: int) -> tuple[dict, bool]:
    """Защитная обрезка конверта результата до остатка бюджета вывода."""
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= cap:
        return result, False
    trimmed = dict(result)
    data = trimmed.get("data")
    if isinstance(data, dict):
        for key in ("stdout", "stderr", "content"):
            if key in data:
                data[key] = str(data[key])[: max(0, cap // 2)] + "\n...[output truncated]"
    trimmed["truncated"] = True
    return trimmed, True


class Orchestrator:
    """Управляет циклом для одного запуска. Все решения — через журнал."""

    def __init__(
        self,
        state: RunState,
        journal: Journal,
        model,
        cfg: Config,
        registry: dict[str, Tool] | None = None,
    ):
        self.state = state
        self.journal = journal
        self.model = model
        self.cfg = cfg
        self.tools = registry or make_tools()
        # Каждое событие журнала немедленно применяется к состоянию:
        # живое состояние всегда равно replay журнала (проверяется тестами).
        journal.on_append = state.apply
        # Счётчик «текстовых» ответов подряд без инструментальных шагов —
        # защита от выжигания бюджета повторяющимися отговорками модели.
        self._stall_turns = 0
        # Точка отказа срабатывает один раз на вызов: retry/resume не должны
        # падать повторно в той же точке.
        self._fired_failpoints: set[str] = set()

    # ------------------------------------------------------------- фазы

    def run(self) -> str:
        """Крутит цикл до остановки. Возвращает статус запуска."""
        state = self.state
        while True:
            if state.status == "awaiting_approval":
                return state.status
            if state.status in TERMINAL_STATUSES:
                return state.status
            # Уровень 10: исходный снимок дерева в начале фазы исполнения.
            if state.phase == "executing" and state.last_tree is None:
                changes.save_snapshot(state, self.journal, self.cfg)
                say(f"  · снимок дерева: {len(state.last_tree or {})} файлов")
            if not model_request_allowed(state.counters, state.limits):
                self._finish(
                    "limit_reached",
                    reason=f"исчерпан лимит model_requests ({state.limits.model_requests})",
                )
                return state.status

            # Списание бюджета ДО обращения к модели; после сбоя не возвращается.
            # Пустой ответ (нет ни текста, ни tool calls — замечено у qwen3.5
            # в Ollama) повторяем один раз; каждый запрос честно списывается.
            attempts = 0
            while True:
                self.journal.append(events.BUDGET_DEBITED, kind="model_request")
                say("Думает…")
                try:
                    reply = self.model.chat(
                        state.messages, tools_for_phase(self.tools, state.phase)
                    )
                except LLMAPIError as err:
                    self._finish("failed", reason=f"ошибка API модели: {err}")
                    return state.status
                except ScriptExhausted as err:
                    self._finish("failed", reason=f"scripted-модель исчерпана: {err}")
                    return state.status
                attempts += 1
                if (reply.text and reply.text.strip()) or reply.tool_calls:
                    break
                if attempts >= 2:
                    break
                say("  · пустой ответ, повторяю запрос…")

            self.journal.append(
                events.MODEL_RESPONSE, turn=state.turn + 1, message=assistant_message(reply)
            )
            if reply.tool_calls:
                self._stall_turns = 0
                self._handle_batch(reply.tool_calls)
                continue

            if reply.text:
                snippet = reply.text.strip()
                if len(snippet) > 600:
                    snippet = snippet[:600] + "…"
                say(f"Ответ: {snippet}")
            if state.phase == "planning" and state.plan is None:
                if self._register_stall():
                    return state.status
                say("  · жду план: модель ответила текстом без submit_plan")
                self.journal.append(
                    events.USER_NOTE,
                    text=(
                        "План ещё не сохранён: сначала изучи проект инструментами "
                        "чтения и вызови submit_plan со структурированным планом."
                    ),
                )
                continue
            open_tasks = state.open_tasks()
            if open_tasks:
                # Уровень 9: финальный текст не закрывает открытые задачи —
                # статус подтверждает только проверка runtime.
                if self._register_stall():
                    return state.status
                say("  · задачи ещё не закрыты — прошу модель продолжить работу")
                current = state.current_task_id or "—"
                pending = ", ".join(t.id for t in open_tasks)
                self.journal.append(
                    events.USER_NOTE,
                    text=(
                        f"Финальный ответ преждевременный: не завершены задачи "
                        f"({pending}), текущая — {current}. Продолжай работу и "
                        "подтверждай результат каждой задачи через complete_task."
                    ),
                )
                continue
            self._finish("completed", final_answer=reply.text or "")
            return state.status

    def approve_plan(self) -> None:
        state = self.state
        if state.status != "awaiting_approval" or state.plan is None:
            raise RuntimeError(f"утверждение плана невозможно в статусе {state.status}")
        text = (
            "План утверждён пользователем. Приступай к исполнению шагов:\n"
            + "\n".join(plan_summary_lines(state.plan))
        )
        self.journal.append(events.PLAN_APPROVED, text=text)
        say("✓ план утверждён — начинается исполнение")
        # Уровень 9: шаги плана становятся задачами runtime.
        tasks.on_plan_approved(state, self.journal)

    def reject_plan(self, reason: str = "") -> None:
        self.journal.append(events.PLAN_REJECTED, reason=reason)
        say("✖ план отклонён")

    def retry_call(self, call_id: str) -> None:
        """Явный повтор неопределённого вызова: новая попытка того же tool call."""
        record = self.state.calls[call_id]
        call = ToolCallRequest(
            id=call_id, name=record.name, arguments=json.loads(record.args_json)
        )
        self._execute(call)

    # ------------------------------------------------------- обработка пачки

    def _handle_batch(self, calls: list[ToolCallRequest]) -> None:
        state = self.state
        prepared: list[ToolCallRequest] = []
        for call in calls:
            if call.parse_error:
                self._refuse(call, PARSE_ERROR, call.parse_error)
                continue
            # Модели иногда передают план JSON-строкой: парсим до валидации,
            # чтобы схема видела объект (иначе отказ на слое схемы).
            if call.name == PLAN_TOOL and isinstance((call.arguments or {}).get("plan"), str):
                try:
                    call.arguments["plan"] = json.loads(call.arguments["plan"])
                except ValueError:
                    pass  # схема отклонит с понятным сообщением
            # Дедупликация и защита протокола (блок 2)
            record = state.calls.get(call.id)
            if record is not None and record.status == "completed":
                if record.name == call.name and record.args_json == _args_json(call):
                    # Обычный повтор tool_call_id: сохранённый результат, без исполнения.
                    self.journal.append(
                        events.TOOL_CALL_REPLAYED, id=call.id, result=record.last_result
                    )
                    say(f"  → {_fmt_call(call)}  [повтор: сохранённый результат]")
                    continue
                self._refuse(
                    call,
                    PROTOCOL_ERROR,
                    "tool_call_id уже использован с другим именем или аргументами",
                )
                continue
            tool = self.tools.get(call.name)
            if tool is None:
                self._refuse(call, UNKNOWN_TOOL, f"инструмент '{call.name}' не зарегистрирован")
                continue
            phase_names = PLANNING_TOOLS if state.phase == "planning" else EXECUTING_TOOLS
            if call.name not in phase_names:
                self._refuse(
                    call,
                    DENIED_BY_POLICY,
                    f"инструмент '{call.name}' недоступен в фазе {state.phase}",
                )
                continue
            error = tool.validate(call.arguments)
            if error:
                self._refuse(call, INVALID_ARGUMENTS, error)
                continue
            prepared.append(call)

        if not prepared:
            return
        # Пограничный случай №1: остатка не хватает на всю пачку — ни один
        # вызов не выполняется, счётчики не списываются.
        flags = [self.tools[c.name].side_effect for c in prepared]
        reason = batch_refusal_reason(state.counters, state.limits, flags)
        if reason is not None:
            for call in prepared:
                self._refuse(
                    call, BUDGET_EXCEEDED, f"исчерпан лимит {reason}: пачка отклонена целиком"
                )
            return

        for call in prepared:
            if state.status == "awaiting_approval":
                # submit_plan уже обработан в этой пачке: остальные вызовы — отказ.
                self._refuse(
                    call, DENIED_BY_POLICY, "после submit_plan ожидается утверждение плана"
                )
                continue
            self._execute(call)

    # --------------------------------------------------------- исполнение

    def _execute(self, call: ToolCallRequest) -> None:
        state = self.state
        tool = self.tools[call.name]
        record = state.calls.get(call.id)
        attempt = len(record.attempts) + 1 if record else 1

        # Пограничный случай №2: остаток бюджета вывода; если он нулевой —
        # вызов не начинается.
        cap = output_cap(state.counters, state.limits)
        if cap <= 0:
            self._refuse(call, BUDGET_EXCEEDED, "исчерпан лимит output_bytes")
            return

        # Уровень 10: перед побочным действием сверяем текущее дерево
        # с последним сохранённым состоянием. Расхождение — конфликт.
        if tool.side_effect and state.last_tree is not None:
            conflict_paths = changes.conflicts(state, self.cfg)
            if conflict_paths:
                self._refuse(
                    call,
                    TREE_MODIFIED_EXTERNALLY,
                    "текущее дерево отличается от последнего сохранённого "
                    "состояния: " + ", ".join(sorted(conflict_paths)[:5]),
                )
                return

        # WAL: до побочного действия фиксируем, что оно началось.
        # Уровень 9: вызов связан с текущей задачей (в планировании task_id нет).
        self.journal.append(
            events.TOOL_CALL_STARTED,
            id=call.id,
            name=call.name,
            args_json=_args_json(call),
            side_effect=tool.side_effect,
            attempt=attempt,
            task_id=state.current_task_id,
        )
        self._failpoint("after_start", call)
        say(f"  → {_fmt_call(call)}")

        if call.name == PLAN_TOOL:
            self._execute_submit_plan(call, attempt)
            return
        if call.name == COMPLETE_TASK_TOOL:
            args = call.arguments or {}
            tasks.complete_task(
                state,
                self.journal,
                self.cfg,
                args.get("task_id", ""),
                call.id,
                attempt,
                override_command=args.get("verification_command"),
            )
            return

        # Уровень 10: снимок дерева ДО побочного действия.
        if tool.side_effect and state.last_tree is not None:
            changes.save_snapshot(state, self.journal, self.cfg)

        ctx = ToolContext(
            workspace=self.cfg.workspace,
            max_output_bytes=cap,
            bash_timeout=self.cfg.bash_timeout,
            bash_path=self.cfg.bash_path,
            protected_paths=self.cfg.protected_paths,
        )
        result = execute(tool, call.arguments or {}, ctx)
        result, truncated = _cap_result(result, cap)

        # Управляемая точка отказа между выполнением и сохранением результата.
        self._failpoint("after_execute", call)

        # Уровень 10: фактический diff и хеши ПОСЛЕ действия.
        if tool.side_effect and state.last_tree is not None:
            changes.record_change(state, self.journal, self.cfg, call.id)

        size = len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
        # Списание за вывод: неусечённый результат — фактический размер;
        # усечённый — весь выделенный остаток (аллокация исчерпана),
        # иначе остаток бюджета никогда не достиг бы нуля.
        used_allocation = truncated or bool((result.get("data") or {}).get("truncated"))
        debited = cap if used_allocation else size
        ok = bool(result.get("ok"))
        self.journal.append(
            events.TOOL_CALL_FINISHED,
            id=call.id,
            attempt=attempt,
            ok=ok,
            size=debited,
            result=result,
        )
        if not ok:
            error = result.get("error", {})
            say(f"      ⚠ {error.get('code', '?')}: {error.get('message', '')[:80]}")

    def _execute_submit_plan(self, call: ToolCallRequest, attempt: int) -> None:
        raw_plan = (call.arguments or {}).get("plan")
        if isinstance(raw_plan, str):
            # Модели иногда передают план строкой — принимаем с автопарсингом.
            try:
                raw_plan = json.loads(raw_plan)
            except ValueError:
                pass
        plan, errors = validate_plan(raw_plan)
        if plan is None:
            result = {
                "ok": False,
                "error": {
                    "code": INVALID_ARGUMENTS,
                    "message": "план невалиден: " + "; ".join(errors),
                    "recoverable": True,
                },
            }
        else:
            result = {"ok": True, "data": {"saved": True, "steps": len(plan["steps"])}}
        size = len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
        self.journal.append(
            events.TOOL_CALL_FINISHED,
            id=call.id,
            attempt=attempt,
            ok=result["ok"],
            size=size,
            result=result,
        )
        if plan is not None:
            self.journal.append(events.PLAN_SAVED, plan=plan)
            say("")
            say("План сохранён — ожидает утверждения:")
            for line in plan_summary_lines(plan):
                say("    " + line)

    def _refuse(self, call: ToolCallRequest, code: str, message: str) -> None:
        recoverable = code != PROTOCOL_ERROR
        result = {
            "ok": False,
            "error": {"code": code, "message": message, "recoverable": recoverable},
        }
        self.journal.append(
            events.TOOL_CALL_REFUSED,
            id=call.id,
            name=call.name,
            args_json=_args_json(call),
            result=result,
        )
        say(f"  → {_fmt_call(call)}  [отклонено: {code}]")
        say(f"      {message[:90]}")

    def _failpoint(self, name: str, call: ToolCallRequest) -> None:
        failpoint = self.cfg.failpoint
        if not failpoint:
            return
        parts = failpoint.split(":", 1)
        if parts[0] != name:
            return
        if len(parts) > 1 and parts[1] != call.id:
            return
        if failpoint in self._fired_failpoints:
            return  # уже срабатывала: retry/resume проходят дальше
        self._fired_failpoints.add(failpoint)
        say(
            f"[failpoint {failpoint}] управляемый сбой на вызове {call.id}; "
            "процесс останавливается"
        )
        raise FailpointCrash(failpoint)

    def _register_stall(self) -> bool:
        """Третий текст-без-инструментов подряд — остановка запуска.

        Повторяющиеся отговорки должны заканчиваться, а не крутиться до
        исчерпания бюджета (принцип конечного бюджета agent loop).
        Возвращает True, если запуск остановлен.
        """
        self._stall_turns += 1
        if self._stall_turns >= 3:
            self._finish(
                "failed",
                reason="модель трижды ответила текстом без инструментальных шагов",
            )
            return True
        return False

    def _finish(self, status: str, reason: str | None = None, final_answer: str | None = None) -> None:
        payload: dict = {"status": status}
        if reason:
            payload["reason"] = reason
        if final_answer is not None:
            payload["final_answer"] = final_answer
        self.journal.append(events.RUN_FINISHED, **payload)
        if status != "completed":
            # Итог успешного запуска печатает вызывающая сторона (CLI/чат).
            say(f"✖ {status}" + (f" ({reason})" if reason else ""))
