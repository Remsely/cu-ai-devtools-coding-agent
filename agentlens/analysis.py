"""Сопоставление неуспешных команд с точными основаниями в журнале."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from agentlens.reader import SessionInput, discover_files, read_session

QUESTION = "Какие неуспешные команды проверок агент повторяет и что делает между ними?"
METHOD = {
    "id": "exact-command-failure-chains-v1",
    "scope": "Bash-команды и фактические проверки complete_task; назначение произвольной Bash-команды не угадывается.",
    "matching": "Точный текст команды, записанный cwd, ожидаемый и фактический коды, таймаут и точная сохранённая диагностика (без внешних пробелов).",
    "chains": "Успех, неизвестный исход или другая диагностика той же команды прерывают цепочку. Пропуски seq и несопоставленные/неоднозначные события прерывают активные цепочки. Действия других команд показаны между повторами.",
    "interpretation": "Повтор наблюдаем; его бесполезность и причина по совпадению не установлены.",
    "limitations": [
        "Разные shell-записи одной операции и разные записи пути не объединяются.",
        "Динамические пути, таймстемпы и изменившийся текст диагностики могут скрыть похожую ошибку.",
        "Усечённая диагностика не используется для точного сопоставления; complete_task сохраняет только хвост вывода.",
        "Журнал не доказывает отсутствие внешних изменений, сетевых эффектов или изменений окружения.",
    ],
}
NONEXECUTION_CODES = {
    "TASK_DEPENDENCIES_NOT_MET", "VERIFICATION_REQUIRED", "INVALID_ARGUMENTS",
    "DENIED_BY_POLICY", "NOT_EXECUTED_AFTER_CRASH",
}


def _ref(row: dict) -> dict:
    return {"seq": row["data"]["seq"], "line": row["line"]}


def _warn(warnings: list[dict], code: str, message: str, row: dict | None = None) -> None:
    warning = {"code": code, "message": message}
    if row is not None:
        warning["event"] = _ref(row)
    warnings.append(warning)


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _integer(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _arguments(event: dict) -> dict:
    value = event.get("args_json", "{}")
    if not isinstance(value, str):
        return {}
    try:
        return _dict(json.loads(value))
    except ValueError:
        return {}


def _payload(result: dict) -> dict:
    if isinstance(result.get("data"), dict):
        return result["data"]
    return _dict(_dict(result.get("error")).get("data"))


def _execution(start: dict, finish: dict | None, task_specs: dict, warnings: list[dict]) -> dict | None:
    event = start["data"]
    tool = event.get("name")
    if tool not in {"Bash", "complete_task"}:
        return None
    args = _arguments(event)
    result = _dict(finish["data"].get("result")) if finish else {}
    payload = _payload(result)
    error_code = _dict(result.get("error")).get("code")
    verification = _dict(payload.get("verification"))
    if tool == "complete_task":
        if payload.get("already_completed") is True:
            return None
        if finish and not verification and error_code in NONEXECUTION_CODES:
            return None
        spec = _dict(task_specs.get(args.get("task_id")))
        command = verification.get("command", args.get("verification_command", spec.get("command")))
        cwd = "."  # run_verification в этом harness всегда исполняется в корне workspace.
        expected = _integer(verification.get("expect_exit_code", spec.get("expect_exit_code", 0)))
        details = verification
        output = verification.get("output") if isinstance(verification.get("output"), str) else None
    else:
        command = args.get("command", payload.get("command"))
        cwd = args.get("cwd", payload.get("cwd", "."))
        expected = 0
        details = payload
        out, err = payload.get("stdout"), payload.get("stderr")
        output = ((out or "") + (err or "")) if isinstance(out, str) and isinstance(err, str) else None
    if not isinstance(command, str) or not command.strip():
        command = None
    if not isinstance(cwd, str) or not cwd:
        cwd = None
    exit_code = _integer(details.get("exit_code"))
    timed_out = details.get("timed_out") is True
    state = "unknown"
    if finish and timed_out:
        state = "failure"
    elif finish and exit_code is not None and expected is not None:
        state = "success" if exit_code == expected else "failure"
    # Решение skip говорит о восстановлении, а не о наблюдавшемся исходе команды.
    if error_code == "UNDEFINED_SKIPPED":
        state = "unknown"
    truncated = result.get("truncated") is True or details.get("truncated") is True
    if tool == "complete_task" and output is not None and len(output) >= 800:
        truncated = True
    signature = None
    if state == "failure" and command is not None and cwd is not None and output is not None and not truncated:
        diagnostic = output.strip()
        signature = {
            "exit_code": exit_code, "expected_exit_code": expected, "timed_out": timed_out,
            "diagnostic": diagnostic,
            "sha256": hashlib.sha256(diagnostic.encode("utf-8")).hexdigest(),
        }
    execution = {
        "call_id": event.get("id"), "attempt": event.get("attempt", 1), "tool": tool,
        "command": command, "cwd": cwd, "state": state, "exit_code": exit_code,
        "expected_exit_code": expected, "timed_out": timed_out, "output": output,
        "signature": signature, "start": _ref(start), "result": _ref(finish) if finish else None,
        "task_id": args.get("task_id", event.get("task_id")),
    }
    if state == "unknown":
        _warn(warnings, "unknown_outcome", f"Исход команды {event.get('id')} (попытка {event.get('attempt', 1)}) неизвестен: нет полного результата с кодом или явным таймаутом.", start)
    if command is None or cwd is None:
        _warn(warnings, "missing_command", f"Недостаточно данных о команде или каталоге вызова {event.get('id')}; точное сопоставление невозможно.", start)
    if truncated:
        _warn(warnings, "truncated_output", f"Диагностика вызова {event.get('id')} усечена или достигла лимита хвоста; одинаковость ошибок по ней не устанавливается.", finish or start)
    elif state == "failure" and output is None:
        _warn(warnings, "missing_output", f"У неуспешного вызова {event.get('id')} нет сохранённой диагностики; сопоставление ошибок невозможно.", finish or start)
    return execution


def _between(previous: dict, current: dict, rows: list[dict]) -> dict:
    first_seq = previous["result"]["seq"] if previous["result"] else previous["start"]["seq"]
    last_seq = current["start"]["seq"]
    actions, changes = [], []
    for row in rows:
        event = row["data"]
        if not first_seq < event["seq"] < last_seq:
            continue
        if event["event"] == "tool_call_started":
            args = _arguments(event)
            actions.append({
                **_ref(row), "call_id": event.get("id"), "attempt": event.get("attempt", 1),
                "tool": event.get("name"),
                "arguments": {key: value for key, value in args.items() if key in {"path", "command", "cwd", "task_id", "verification_command"}},
            })
        elif event["event"] == "change_recorded":
            for change in event.get("diff", []) if isinstance(event.get("diff"), list) else []:
                if isinstance(change, dict) and isinstance(change.get("path"), str):
                    changes.append({
                        **_ref(row), "call_id": event.get("call_id"), "path": change["path"],
                        "change": change.get("change"), "before_hash": change.get("before_hash"),
                        "after_hash": change.get("after_hash"),
                    })
    return {"from": previous["result"], "to": current["start"], "actions": actions, "changes": changes}


def _findings(executions: list[dict], rows: list[dict], uncertain_sequences: list[int]) -> list[dict]:
    chains: dict[tuple, list[dict]] = {}
    findings: list[dict] = []
    gaps = [row["data"]["seq"] for before, row in zip(rows, rows[1:]) if row["data"]["seq"] != before["data"]["seq"] + 1]
    barriers = gaps + uncertain_sequences
    previous_boundary = 0

    def flush(key: tuple) -> None:
        chain = chains.pop(key, [])
        if len(chain) < 2:
            return
        findings.append({
            "command": chain[0]["command"], "cwd": chain[0]["cwd"], "signature": chain[0]["signature"],
            "occurrences": chain, "repeat_count": len(chain) - 1,
            "between": [_between(before, after, rows) for before, after in zip(chain, chain[1:])],
            "interpretation": "Команда повторно дала тот же сохранённый неуспешный результат. Причина ошибки и полезность повторов требуют отдельной проверки.",
        })

    for execution in executions:
        boundary = (execution["result"] or execution["start"])["seq"]
        if execution["command"] is None or execution["cwd"] is None or any(previous_boundary < barrier <= boundary for barrier in barriers):
            for pending in list(chains):
                flush(pending)
        previous_boundary = max(previous_boundary, boundary)
        key = (execution["command"], execution["cwd"])
        if execution["state"] != "failure" or execution["signature"] is None:
            flush(key)
            continue
        existing = chains.get(key)
        if existing and existing[-1]["signature"] != execution["signature"]:
            flush(key)
        chains.setdefault(key, []).append(execution)
    for key in list(chains):
        flush(key)
    return sorted(findings, key=lambda finding: finding["occurrences"][0]["start"]["seq"])


def analyze_session(source: SessionInput) -> dict | None:
    if not source.events:
        return None
    rows = source.events
    initial = rows[0]["data"]
    session_id = initial.get("run_id") or Path(source.source).parent.name
    warnings: list[dict] = []
    starts: list[dict] = []
    results: dict[tuple, dict] = {}
    task_specs: dict = {}
    seen_starts: set[tuple] = set()
    ambiguous: set[tuple] = set()
    uncertain_sequences: list[int] = []
    status = "incomplete"
    replay_count = 0
    refused_count = 0
    previous_seq = 0
    for row in rows:
        event = row["data"]
        if event["seq"] != previous_seq + 1:
            _warn(warnings, "sequence_gap", f"Перед seq={event['seq']} есть пропуск событий; контекст между действиями может быть неполным.", row)
        previous_seq = event["seq"]
        kind = event["event"]
        if kind == "tasks_created":
            for task in event.get("tasks", []) if isinstance(event.get("tasks"), list) else []:
                if isinstance(task, dict) and isinstance(task.get("id"), str):
                    task_specs[task["id"]] = _dict(task.get("verification"))
        elif kind == "tool_call_started":
            key = (event.get("id"), event.get("attempt", 1))
            try:
                hash(key)
            except TypeError:
                _warn(warnings, "invalid_call", "ID вызова/номер попытки имеет некорректный тип.", row)
                uncertain_sequences.append(event["seq"])
                continue
            if key in seen_starts:
                ambiguous.add(key)
                uncertain_sequences.append(event["seq"])
                _warn(warnings, "duplicate_start", f"Повторная запись started для {key}; исход этой попытки неоднозначен.", row)
            else:
                starts.append(row)
                seen_starts.add(key)
        elif kind == "tool_call_finished":
            key = (event.get("id"), event.get("attempt", 1))
            try:
                hash(key)
            except TypeError:
                _warn(warnings, "invalid_call", "ID результата/номер попытки имеет некорректный тип.", row)
                uncertain_sequences.append(event["seq"])
                continue
            if key in results:
                ambiguous.add(key)
                uncertain_sequences.append(event["seq"])
                _warn(warnings, "duplicate_result", f"Несколько результатов одной попытки {key}; исход неоднозначен.", row)
            results[key] = row
            if key not in seen_starts:
                uncertain_sequences.append(event["seq"])
                _warn(warnings, "orphan_result", f"У результата {key} отсутствует предшествующий started; исполнение не учитывается.", row)
        elif kind == "tool_call_replayed":
            replay_count += 1
        elif kind == "tool_call_refused":
            refused_count += 1
        elif kind == "run_finished":
            status = str(event.get("status", "unknown"))
    if status == "incomplete":
        _warn(warnings, "missing_run_finished", "Нет run_finished: сессия могла быть прервана; итоговый статус неизвестен.")
    executions = []
    for start in starts:
        event = start["data"]
        key = (event.get("id"), event.get("attempt", 1))
        finish = results.get(key) if key not in ambiguous else None
        if finish and finish["data"]["seq"] <= event["seq"]:
            _warn(warnings, "result_before_start", f"Результат {key} записан до started и не подтверждает исход этой попытки.", start)
            finish = None
        execution = _execution(start, finish, task_specs, warnings)
        if execution is not None:
            executions.append(execution)
    findings = _findings(executions, rows, uncertain_sequences)
    return {
        "session_id": str(session_id), "source": source.source, "workspace": initial.get("workspace"),
        "task": initial.get("task"), "status": status, "complete": not source.errors and not warnings,
        "warnings": warnings, "executions": executions, "findings": findings,
        "replayed_calls": replay_count, "refused_calls": refused_count,
    }


def analyze_path(path: str | Path) -> dict:
    """Возвращает единый JSON-совместимый отчёт, включая ошибки отдельных файлов."""
    input_path = Path(path).expanduser()
    files, errors = discover_files(input_path)
    sessions = []
    seen_ids: set[str] = set()
    for file in files:
        source = read_session(file)
        errors.extend(source.errors)
        session = analyze_session(source)
        if session is not None:
            if session["session_id"] in seen_ids:
                errors.append({"source": str(file), "message": f"Повтор session_id={session['session_id']}; дубликат не включён в выборку."})
                continue
            seen_ids.add(session["session_id"])
            sessions.append(session)
    executions = [execution for session in sessions for execution in session["executions"]]
    findings = [finding for session in sessions for finding in session["findings"]]
    summary = {
        "files_seen": len(files), "sessions_analyzed": len(sessions),
        "sessions_with_repeats": sum(bool(session["findings"]) for session in sessions),
        "executions": len(executions),
        "failed_executions": sum(execution["state"] == "failure" for execution in executions),
        "unknown_executions": sum(execution["state"] == "unknown" for execution in executions),
        "uncomparable_failures": sum(execution["state"] == "failure" and execution["signature"] is None for execution in executions),
        "repeat_episodes": len(findings), "repeated_failures": sum(finding["repeat_count"] for finding in findings),
        "incomplete_sessions": sum(not session["complete"] for session in sessions),
    }
    return {
        "schema_version": 1, "question": QUESTION, "method": METHOD, "input": str(input_path),
        "complete": not errors and all(session["complete"] for session in sessions),
        "summary": summary, "sessions": sessions, "errors": errors,
    }
