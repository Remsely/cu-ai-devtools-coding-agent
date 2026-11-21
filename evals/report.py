"""Офлайн-сводка сохранённых экспериментов с проверкой сопоставимости условий."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import sys
import zipfile

from agentlens import analyze_path


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


def _load(path: Path, issues: list[dict]):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Ожидался JSON-объект")
        return value
    except (OSError, ValueError) as err:
        issues.append({"code": "unreadable_record", "source": str(path),
                       "message": f"{type(err).__name__}: {err}"})
        return None


def _journal_metadata(path: Path, meta: dict, issues: list[dict], source: str) -> dict:
    """Сверяет сохраняемые метаданные с первичными событиями, не меняя журнал."""
    first = None
    terminal = None
    tool_calls = model_requests = 0
    try:
        with path.open(encoding="utf-8-sig") as stream:
            for line in stream:
                if not line.strip():
                    continue
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError("Событие не является JSON-объектом")
                if first is None:
                    first = event
                if event.get("event") == "run_finished":
                    terminal = event
                tool_calls += event.get("event") == "tool_call_started"
                model_requests += event.get("event") == "budget_debited" and event.get("kind") == "model_request"
    except (OSError, ValueError) as err:
        issues.append({"code": "journal_metadata_unavailable", "source": source,
                       "message": f"Не удалось сверить первичные события: {err}"})
        return {}
    if not first or first.get("event") != "run_started":
        issues.append({"code": "journal_start_missing", "source": source,
                       "message": "Первое событие журнала не является run_started."})
    else:
        if first.get("run_id") != meta.get("run_id"):
            issues.append({"code": "journal_run_id_mismatch", "source": source,
                           "message": "run_id первичного события расходится с manifest."})
        if first.get("initial_messages") != meta.get("initial_messages"):
            issues.append({"code": "journal_context_mismatch", "source": source,
                           "message": "Начальный контекст в журнале расходится с manifest."})
    return {"terminal": terminal, "tool_calls": tool_calls, "model_requests": model_requests}


def _verify_harness_archive(directory: Path, meta: dict, issues: list[dict], source: str) -> None:
    archive_path = directory / "harness.zip"
    if not archive_path.exists():
        return
    try:
        with zipfile.ZipFile(archive_path) as archive:
            entries = archive.infolist()
            if sum(entry.file_size for entry in entries) > 10_000_000:
                raise ValueError("Архив harness превышает допустимый размер 10 МБ")
            actual = {entry.filename.removeprefix("agent/"): hashlib.sha256(archive.read(entry)).hexdigest()
                      for entry in entries if not entry.is_dir()}
        if actual != meta.get("harness_hashes"):
            issues.append({"code": "harness_archive_mismatch", "source": source,
                           "message": "Байты сохранённого harness.zip не совпадают с manifest.harness_hashes."})
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as err:
        issues.append({"code": "harness_archive_unreadable", "source": source,
                       "message": f"Не удалось проверить архив harness: {err}"})


def _aggregate(rows: list[dict], task: str, variant: str) -> dict:
    result = {"task_id": task, "variant": variant, "runs": len(rows),
              "artifact_successes": sum(r["artifact_success"] is True for r in rows),
              "artifact_failures": sum(r["artifact_success"] is False for r in rows),
              "unknown_artifacts": sum(r["artifact_success"] is None for r in rows),
              "agent_completed": sum(r["agent_completed"] is True for r in rows),
              "agent_noncompleted": sum(r["agent_completed"] is False for r in rows),
              "agent_completion_unknown": sum(r["agent_completed"] is None for r in rows)}
    for field in ("repeated_failures", "full_time_s", "tool_calls", "model_requests"):
        values = [r[field] for r in rows if r[field] is not None]
        result[field] = {"median": round(statistics.median(values), 3) if values else None,
                         "sum": sum(values) if values else None,
                         "measured_runs": len(values), "unknown_runs": len(rows) - len(values)}
    return result


def _validate_conditions(records: list[tuple[dict, dict]], comparison: bool, issues: list[dict]) -> dict:
    def issue(code, message, source=""):
        issues.append({"code": code, "source": source, "message": message})

    if comparison:
        expected = {(task, variant): 3 for task in ("shipping", "quantity")
                    for variant in ("baseline", "changed")}
        actual = Counter((row["task_id"], row["variant"]) for row, _ in records)
        if actual != expected:
            issue("matrix_mismatch", "Ожидалось 12 попыток: shipping/quantity × baseline/changed × 3. "
                  f"Получено: {dict(actual)}")
    if not records:
        issue("no_trials", "Не найдено ни одной попытки с manifest.json.")
        return {"comparison_requested": comparison, "comparable": False}

    ids = [r["run_id"] for r, _ in records if r["run_id"]]
    if len(ids) != len(set(ids)):
        issue("duplicate_run_id", "Один run_id встречается более одного раза; попытки не независимы.")
    common_fields = ("model", "base_url", "model_parameters", "limits", "wall_timeout_s",
                     "image_id", "isolation", "harness_hashes", "grader_sha256", "runner_sha256")
    groups = [("all", records)] if comparison else [
        (task, [(row, meta) for row, meta in records if row["task_id"] == task])
        for task in sorted({r["task_id"] for r, _ in records})]
    for group, items in groups:
        for field in common_fields:
            missing = [r["source"] for r, m in items if field not in m or m[field] is None]
            if missing:
                issue("missing_condition", f"Условие {field!r} неизвестно: {', '.join(missing)}", group)
            values = {json.dumps(m[field], sort_keys=True, ensure_ascii=False)
                      for _, m in items if field in m and m[field] is not None}
            if len(values) > 1:
                issue("condition_differs", f"Условие {field!r} различается между попытками.", group)
    for task in sorted({r["task_id"] for r, _ in records}):
        items = [(r, m) for r, m in records if r["task_id"] == task]
        for field in ("initial_hashes", "task_sha256"):
            if any(not m.get(field) for _, m in items):
                issue("missing_initial_condition", f"Нет полного {field!r} для задачи {task}.")
            values = {json.dumps(m[field], sort_keys=True) for _, m in items if m.get(field)}
            if len(values) > 1:
                issue("initial_condition_differs", f"Исходное состояние {field!r} задачи {task} различается.")
    instructions = {}
    for variant in sorted({r["variant"] for r, _ in records}):
        items = [(r, m) for r, m in records if r["variant"] == variant]
        if any("instruction" not in m for _, m in items):
            issue("missing_instruction", f"Неизвестна инструкция варианта {variant}.")
        if any("instruction" in m and not isinstance(m["instruction"], str) for _, m in items):
            issue("invalid_instruction", f"Инструкция варианта {variant} должна быть строкой.")
        variants = {m.get("instruction") for _, m in items if isinstance(m.get("instruction"), str)}
        if len(variants) > 1:
            issue("instruction_differs", f"Инструкция менялась внутри варианта {variant}.")
        instructions[variant] = next(iter(variants)) if len(variants) == 1 else None
    changed_instruction = instructions.get("changed")
    if comparison and (not isinstance(changed_instruction, str) or not changed_instruction.strip()
                       or instructions.get("baseline") != ""):
        issue("unexpected_variant_difference", "Ожидались исходная инструкция A и одна непустая добавка B.")
    for row, meta in records:
        messages = meta.get("initial_messages")
        if not isinstance(messages, list) or len(messages) != 2:
            issue("initial_context_unavailable", "Невозможно проверить полный начальный контекст.", row["source"])
        elif any(not isinstance(message, dict) or not isinstance(message.get("content"), str)
                 or not message["content"].strip() for message in messages):
            issue("invalid_initial_context", "Ожидались два непустых текстовых сообщения.", row["source"])
        elif [message.get("role") for message in messages] != ["system", "user"]:
            issue("invalid_initial_roles", "Начальные роли должны быть system, user именно в этом порядке.", row["source"])
    if comparison:
        for task in sorted({r["task_id"] for r, _ in records}):
            canonical = []
            for row, meta in records:
                if row["task_id"] != task or not isinstance(meta.get("initial_messages"), list):
                    continue
                messages = json.loads(json.dumps(meta["initial_messages"]))
                if len(messages) != 2 or not isinstance(messages[0], dict):
                    continue
                suffix = meta.get("instruction") if isinstance(meta.get("instruction"), str) else ""
                content = messages[0].get("content")
                if not isinstance(content, str):
                    issue("invalid_initial_context", "Нет текста системной инструкции.", row["source"])
                    continue
                if suffix:
                    if not content.endswith("\n\n" + suffix):
                        issue("instruction_not_in_context", "Заявленная добавка отсутствует в начальном контексте.", row["source"])
                        continue
                    messages[0]["content"] = content[:-len("\n\n" + suffix)]
                canonical.append(json.dumps(messages, sort_keys=True, ensure_ascii=False))
            if len(set(canonical)) > 1:
                issue("extra_context_difference", f"Кроме заявленной добавки, контекст задачи {task} различается.")
    return {"comparison_requested": comparison, "comparable": not issues,
            "checked_conditions": list(common_fields) + ["initial_hashes", "task_sha256", "initial_messages"],
            "instructions": instructions,
            "scope": "Проверены сохранённые метаданные; это не доказательство отсутствия скрытых факторов."}


def build_report(source: str | Path, *, comparison: bool = False, notes: list[str] | None = None) -> dict:
    source = Path(source).resolve()
    issues: list[dict] = []
    if not source.is_dir():
        raise ValueError(f"Каталог попыток не найден: {source}")
    records = []
    for manifest_path in sorted(source.rglob("manifest.json")):
        directory = manifest_path.parent
        meta = _load(manifest_path, issues)
        if meta is None:
            continue
        for field in ("task_id", "variant", "run_id"):
            if not isinstance(meta.get(field), str) or not meta[field]:
                issues.append({"code": "invalid_identity", "source": str(manifest_path),
                               "message": f"Поле {field!r} должно быть непустой строкой."})
                meta[field] = "unknown" if field != "run_id" else None
        result = _load(directory / "result.json", issues) or {}
        grade = result.get("grade") if isinstance(result.get("grade"), dict) else {}
        status = result.get("agent_status")
        if grade.get("status") not in ("pass", "fail", "error"):
            issues.append({"code": "grade_unknown", "source": str(directory),
                           "message": "Результат независимой проверки неизвестен."})
        if not isinstance(status, str) or not status:
            issues.append({"code": "agent_status_unknown", "source": str(directory),
                           "message": "Состояние агента неизвестно."})
        completed = result.get("agent_completed")
        inferred = []
        if not isinstance(completed, bool):
            completed = status == "completed" if isinstance(status, str) and status else None
            if completed is not None:
                inferred.append("agent_completed: выведено из сохранённого agent_status")
        interrupted = result.get("externally_interrupted")
        if not isinstance(interrupted, bool):
            interrupted = status == "interrupted" if isinstance(status, str) and status else None
            if interrupted is not None:
                inferred.append("externally_interrupted: выведено из сохранённого agent_status")
        violations = result.get("restriction_violations")
        if not isinstance(violations, list):
            violations = None
        success = None
        if grade.get("status") == "fail" or violations:
            success = False
        elif grade.get("status") == "pass" and violations is not None:
            success = True
        run_id = meta.get("run_id")
        row = {"source": directory.relative_to(source).as_posix(), "task_id": meta.get("task_id", "unknown"),
               "variant": meta.get("variant", "unknown"), "run_id": run_id,
               "independent_grade": grade.get("status", "unknown"), "artifact_success": success,
               "independent_result": grade,
               "agent_status": status, "agent_completed": completed,
               "externally_interrupted": interrupted, "inferred_fields": inferred,
               "restriction_violations": violations, "error": result.get("error"), "stop_reason": None,
               "full_time_s": _number(result.get("full_time_s")),
               "tool_calls": _number(result.get("tool_calls")),
               "model_requests": _number(result.get("model_requests")),
               "tokens": _number(result.get("tokens")),
               "tokens_unknown_reason": result.get("tokens_unknown_reason", "Нет сохранённого измерения."),
               "repeated_failures": None, "repeat_episodes": None,
               "failed_executions": None, "unknown_executions": None, "analysis_complete": False,
               "journal": None, "analysis": None}
        journals = list(directory.glob("trace/*/journal.jsonl")) + list(directory.glob("runs/*/journal.jsonl"))
        if len(journals) != 1:
            issues.append({"code": "journal_count", "source": row["source"],
                           "message": f"Ожидался один журнал, найдено {len(journals)}."})
        else:
            journal = journals[0]
            if journal.parent.name != run_id:
                issues.append({"code": "journal_identity_mismatch", "source": row["source"],
                               "message": "Каталог журнала не совпадает с run_id из manifest."})
            row["journal"] = journal.relative_to(source).as_posix()
            metadata = _journal_metadata(journal, meta, issues, row["source"])
            terminal = metadata.get("terminal") or {}
            row["stop_reason"] = terminal.get("reason")
            if terminal.get("status") and terminal["status"] != status:
                issues.append({"code": "terminal_status_mismatch", "source": row["source"],
                               "message": "Итоговый статус первичного события расходится с result.json."})
            for field in ("tool_calls", "model_requests"):
                if metadata.get(field) is not None and row[field] is not None and row[field] != metadata[field]:
                    issues.append({"code": "counter_mismatch", "source": row["source"],
                                   "message": f"Счётчик {field!r} расходится с первичными событиями."})
            analysis = analyze_path(journal)
            row["analysis"] = analysis
            row["analysis_complete"] = analysis.get("complete") is True
            summary = analysis.get("summary", {})
            if not isinstance(summary, dict):
                summary = {}
            row["repeated_failures"] = _number(summary.get("repeated_failures"))
            row["repeat_episodes"] = _number(summary.get("repeat_episodes"))
            row["failed_executions"] = _number(summary.get("failed_executions"))
            row["unknown_executions"] = _number(summary.get("unknown_executions"))
            if not row["analysis_complete"]:
                issues.append({"code": "incomplete_analysis", "source": row["source"],
                               "message": "Журнал проанализирован не полностью; метрика имеет ограничения."})
        for field in ("task_id", "variant", "run_id"):
            if result and result.get(field) != meta.get(field):
                issues.append({"code": "record_mismatch", "source": row["source"],
                               "message": f"Поле {field!r} расходится между manifest и result."})
        _verify_harness_archive(directory, meta, issues, row["source"])
        records.append((row, meta))
    conditions = _validate_conditions(records, comparison, issues)
    rows = [row for row, _ in records]
    aggregates = []
    for task in sorted({r["task_id"] for r in rows} | {"all"}):
        for variant in sorted({r["variant"] for r in rows}):
            selected = [r for r in rows if r["variant"] == variant and (task == "all" or r["task_id"] == task)]
            if selected:
                aggregates.append(_aggregate(selected, task, variant))
    return {"schema_version": "1.0", "input": str(source), "runs": rows,
            "aggregates": aggregates, "conditions": conditions, "issues": issues,
            "provenance_notes": notes or [],
            "interpretation": "Победитель автоматически не определяется. Сопоставляйте повторы с независимым результатом, "
                              "завершением агента, затратами и ограничениями. Малая выборка не доказывает статистическую значимость.",
            "metric": "repeated_failures: наблюдаемое число повторных неудачных исполнений согласно методу AgentLens; "
                      "повтор не доказывает бесполезность действия или причину ошибки."}


def markdown(report: dict, output_dir: Path | None = None) -> str:
    def cell(value):
        if value is None:
            return "неизвестно"
        if isinstance(value, bool):
            return "да" if value else "нет"
        return str(value).replace("|", "\\|").replace("\n", " ")

    title = "# Сравнение A/B" if report["conditions"]["comparison_requested"] else "# Исходные наблюдения"
    lines = [title, "", report["metric"], "",
             "Успех артефакта означает прохождение независимых проверок без наблюдаемых нарушений ограничений. "
             "Завершение агента показано отдельно. Неизвестные измерения не заменяются нулём.", "",
             "| Попытка | Задача | Вариант | Grader | Агент | Артефакт успешен | Повторы ошибок | Журнал полный | Время, с | Tool calls | Model calls | Нарушения |",
             "|---|---|---|---|---|---|---:|---|---:|---:|---:|---|"]
    for row in report["runs"]:
        label = row["source"]
        if output_dir is not None and row["journal"]:
            target = Path(report["input"]) / row["journal"]
            try:
                relative = Path(os.path.relpath(target, output_dir)).as_posix()
            except ValueError:  # Разные диски в Windows.
                relative = target.as_posix()
            label = f"[{label}](<{relative}>)"
        fields = [label, row["task_id"], row["variant"], row["independent_grade"],
                  row["agent_status"], row["artifact_success"], row["repeated_failures"], row["analysis_complete"],
                  row["full_time_s"], row["tool_calls"], row["model_requests"], row["restriction_violations"]]
        lines.append("| " + " | ".join(cell(v) for v in fields) + " |")
    lines += ["", "## Сводка", "",
              "| Задача | Вариант | Попыток | Успешных артефактов | Неизвестный результат | Агент завершён | Повторы, медиана | Время, медиана | Tools, медиана |",
              "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for item in report["aggregates"]:
        fields = [item["task_id"], item["variant"], item["runs"], item["artifact_successes"],
                  item["unknown_artifacts"], item["agent_completed"], item["repeated_failures"]["median"],
                  item["full_time_s"]["median"], item["tool_calls"]["median"]]
        lines.append("| " + " | ".join(cell(v) for v in fields) + " |")
    lines += ["", "Для каждой медианы JSON содержит число измеренных и неизвестных значений. "
              "Токены и причины отсутствия измерений сохранены по попыткам в JSON."]
    stopped = [row for row in report["runs"] if row["stop_reason"] or row["error"]]
    if stopped:
        lines += ["", "## Причины остановки", ""]
        lines += [f"- {cell(row['source'])} ({cell(row['agent_status'])}): {cell(row['stop_reason'] or row['error'])}" for row in stopped]
    lines += ["", "## Проверка условий", "",
              "Сохранённые условия сопоставимы: " + cell(report["conditions"]["comparable"]) + ".", ""]
    if report["issues"]:
        lines += [f"- `{item['code']}` ({cell(item['source'])}): {cell(item['message'])}" for item in report["issues"]]
    else:
        lines.append("Расхождений в проверенных метаданных не обнаружено.")
    if report["provenance_notes"]:
        lines += ["", "## Ограничения происхождения данных", ""]
        lines += ["- " + note for note in report["provenance_notes"]]
    lines += ["", "## Интерпретация", "", report["interpretation"], "",
              "Полные основания AgentLens, пути журналов и все результаты сохранены в соседнем report.json.", ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Офлайн-отчёт по сохранённым попыткам агента.")
    parser.add_argument("--input", required=True, help="Каталог с manifest.json/result.json и журналами попыток.")
    parser.add_argument("--output", required=True, help="Каталог для report.json и report.md; отчёты можно пересоздавать.")
    parser.add_argument("--comparison", action="store_true", help="Проверить матрицу shipping/quantity × A/B × 3 и одинаковые условия.")
    parser.add_argument("--note", action="append", default=[], help="Явное ограничение происхождения данных; можно повторять.")
    args = parser.parse_args(argv)
    try:
        report = build_report(args.input, comparison=args.comparison, notes=args.note)
        output = Path(args.output).resolve()
        output.mkdir(parents=True, exist_ok=True)
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (output / "report.md").write_text(markdown(report, output), encoding="utf-8")
    except (OSError, ValueError) as err:
        print(f"Не удалось построить отчёт: {err}", file=sys.stderr)
        return 2
    print(f"Отчёт: {output / 'report.md'}; JSON: {output / 'report.json'}; замечаний: {len(report['issues'])}.")
    return 1 if report["issues"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
