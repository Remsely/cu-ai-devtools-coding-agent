"""Чтение журналов без запуска агента, инструментов или модельного клиента."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path


def _reject_constant(value: str) -> None:
    raise ValueError(f"{value} не является значением JSON")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("число выходит за диапазон конечных значений JSON")
    return number


def _validate_utf8(value) -> None:
    """Проверяет декодированные строки и ключи без рекурсивного обхода."""
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            try:
                item.encode("utf-8")
            except UnicodeEncodeError as error:
                raise ValueError("строка содержит непарный суррогат Unicode и не представима в UTF-8") from error
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


@dataclass
class SessionInput:
    source: str
    events: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)


def _validate_event(event: dict) -> None:
    """Проверяет используемые поля; дополнительные поля совместимы с форматом."""
    kind = event["event"]
    if kind == "run_started" and (
        not isinstance(event.get("run_id"), str) or not event["run_id"].strip()
    ):
        raise ValueError("run_id сессии должен быть непустой строкой")
    if kind == "run_finished" and (
        not isinstance(event.get("status"), str) or not event["status"].strip()
    ):
        raise ValueError("status завершённой сессии должен быть непустой строкой")
    if kind in {"tool_call_started", "tool_call_finished"}:
        if not isinstance(event.get("id"), str) or not event["id"]:
            raise ValueError("id вызова должен быть непустой строкой")
        attempt = event.get("attempt", 1)
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise ValueError("attempt должен быть положительным целым числом")
    if kind == "tool_call_started":
        if not isinstance(event.get("name"), str) or not event["name"]:
            raise ValueError("name инструмента должен быть непустой строкой")
        raw_args = event.get("args_json")
        if not isinstance(raw_args, str):
            raise ValueError("args_json должен быть строкой с JSON-объектом")
        arguments = json.loads(raw_args, parse_constant=_reject_constant, parse_float=_finite_float)
        _validate_utf8(arguments)
        if not isinstance(arguments, dict):
            raise ValueError("args_json должен содержать JSON-объект")
        if event["name"] == "Bash":
            if not isinstance(arguments.get("command"), str) or not arguments["command"].strip():
                raise ValueError("Bash.command должен быть непустой строкой")
            if "cwd" in arguments and not isinstance(arguments["cwd"], str):
                raise ValueError("Bash.cwd должен быть строкой")
        if "task_id" in arguments and not isinstance(arguments["task_id"], str):
            raise ValueError("task_id должен быть строкой")
    elif kind == "tool_call_finished":
        result = event.get("result")
        if not isinstance(result, dict):
            raise ValueError("result должен быть JSON-объектом")
        for key in ("data", "error"):
            if key in result and not isinstance(result[key], dict):
                raise ValueError(f"result.{key} должен быть JSON-объектом")
        error = result.get("error", {})
        if "code" in error and (not isinstance(error["code"], str) or not error["code"]):
            raise ValueError("result.error.code должен быть непустой строкой")
        if "data" in error and not isinstance(error["data"], dict):
            raise ValueError("result.error.data должен быть JSON-объектом")
        payload = result.get("data", error.get("data", {}))
        if "verification" in payload and not isinstance(payload["verification"], dict):
            raise ValueError("verification должен быть JSON-объектом")
        details = payload.get("verification", payload)
        for key in ("exit_code", "expect_exit_code"):
            value = details.get(key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise ValueError(f"{key} должен быть целым числом или null")
        for key in ("command", "cwd", "stdout", "stderr", "output"):
            if key in details and not isinstance(details[key], str):
                raise ValueError(f"{key} должен быть строкой")
        for key in ("timed_out", "truncated"):
            if key in details and not isinstance(details[key], bool):
                raise ValueError(f"{key} должен быть boolean")


def discover_files(path: Path) -> tuple[list[Path], list[dict]]:
    """Один файл или все *.jsonl в каталоге; ошибки обхода не скрываются."""
    if path.is_file():
        return [path], []
    if not path.exists():
        return [], [{"source": str(path), "message": "Входной путь не существует."}]
    if not path.is_dir():
        return [], [{"source": str(path), "message": "Ожидался файл или каталог журналов."}]
    errors: list[dict] = []
    files: list[Path] = []

    def on_error(error: OSError) -> None:
        errors.append({"source": str(error.filename or path), "message": str(error)})

    for directory, subdirs, names in os.walk(path, onerror=on_error, followlinks=False):
        subdirs.sort()
        for name in sorted(names):
            if name.lower().endswith(".jsonl"):
                files.append(Path(directory) / name)
    if not files and not errors:
        errors.append({"source": str(path), "message": "В каталоге нет файлов *.jsonl."})
    return sorted(files), errors


def read_session(path: Path) -> SessionInput:
    """Сохраняет номера строк и корректный префикс повреждённого журнала."""
    session = SessionInput(source=str(path))
    try:
        content = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as error:
        session.errors.append({"source": str(path), "message": f"Не удалось прочитать журнал: {error}"})
        return session
    previous_seq = 0
    for line_number, line in enumerate(content.splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line, parse_constant=_reject_constant, parse_float=_finite_float)
            _validate_utf8(event)
            if not isinstance(event, dict):
                raise ValueError("событие должно быть JSON-объектом")
            if not isinstance(event.get("event"), str) or not event["event"]:
                raise ValueError("нужно непустое строковое поле event")
            seq = event.get("seq")
            if isinstance(seq, bool) or not isinstance(seq, int) or seq <= previous_seq:
                raise ValueError("seq должен быть положительным строго возрастающим целым числом")
            _validate_event(event)
        except (ValueError, TypeError, RecursionError) as error:
            session.errors.append({
                "source": str(path), "line": line_number,
                "message": f"Некорректное событие в строке {line_number}: {error}",
            })
            break
        previous_seq = seq
        session.events.append({"data": event, "line": line_number})
    if not session.events and not session.errors:
        session.errors.append({"source": str(path), "message": "Журнал пуст: нет событий."})
    if session.events:
        starts = [row for row in session.events if row["data"]["event"] == "run_started"]
        if len(starts) != 1 or session.events[0]["data"]["event"] != "run_started":
            session.errors.append({
                "source": str(path),
                "message": "Файл должен содержать одну сессию и начинаться событием run_started.",
            })
            session.events.clear()
    return session
