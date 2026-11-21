"""Терминальный и JSON-интерфейс, независимый от модельного доступа."""

from __future__ import annotations

import argparse
import json
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentlens",
        description="Разбор работы coding agent по сохранённым журналам, без модели и сети.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    analyze = subparsers.add_parser(
        "analyze",
        help="проанализировать одну сессию или набор сессий",
        description=(
            "Вход: UTF-8 JSONL-журнал coding agent (по одному событию в строке, "
            "поля event/seq; одна сессия с run_started) или каталог. "
            "В каталоге рекурсивно читаются только *.jsonl; snapshots/*.json не нужны. "
            "Анализ не исполняет команды из журнала и не требует API-ключа."
        ),
        epilog=(
            "Примеры: python -m agentlens analyze --input runs/RUN_ID/journal.jsonl\n"
            "         python -m agentlens analyze --input evals/sessions/discovery --json\n"
            "Коды выхода: 0 — анализ завершён (в том числе без находок); "
            "2 — некорректный или нечитаемый вход. "
            "Оборванная сессия с корректными событиями даёт предупреждения."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    analyze.add_argument("--input", required=True, metavar="PATH", help="файл журнала или каталог *.jsonl")
    analyze.add_argument("--json", action="store_true", help="один JSON-объект в stdout; ошибки — в stderr")
    return parser


def _address(reference: dict | None) -> str:
    if not reference:
        return "результат отсутствует"
    return f"seq={reference.get('seq', '?')}, строка {reference.get('line', '?')}"


def render_text(report: dict) -> str:
    """Компактная сводка с основаниями; полный вывод остаётся в JSON и журнале."""
    summary = report["summary"]
    lines = [
        "AgentLens — разбор журналов",
        report["question"],
        "",
        f"Файлов: {summary['files_seen']}; сессий разобрано: {summary['sessions_analyzed']}; "
        f"сессий с повторами: {summary['sessions_with_repeats']}.",
        f"Попыток команд: {summary['executions']}; неуспешных: {summary['failed_executions']}; "
        f"с неизвестным исходом: {summary['unknown_executions']}.",
        f"Эпизодов повторения: {summary['repeat_episodes']}; "
        f"повторов после первой ошибки: {summary.get('repeated_failures', 0)}.",
    ]
    if not report["complete"]:
        lines.append("Сводка неполная: см. предупреждения и ошибки ниже.")
    for session in report["sessions"]:
        lines.extend(["", f"Сессия {session['session_id']} ({session['status']})", f"  Файл: {session['source']}"])
        if not session["findings"]:
            lines.append("  Повторных одинаковых неудачных команд по правилу метода не найдено.")
        for finding in session["findings"]:
            lines.append(f"  Команда: {finding['command']}")
            lines.append(f"  Каталог: {finding['cwd']}; повторов: {finding['repeat_count']}")
            occurrences = finding["occurrences"]
            shown = occurrences if len(occurrences) <= 3 else occurrences[:2] + occurrences[-1:]
            for index, occurrence in enumerate(shown):
                if len(occurrences) > 3 and index == 2:
                    lines.append(f"    … ещё {len(occurrences) - 3} попыток; все адреса и результаты — в JSON")
                lines.append(
                    f"    {occurrence['call_id']}, попытка {occurrence['attempt']}, "
                    f"{occurrence['tool']}: {_address(occurrence['start'])} → "
                    f"{_address(occurrence['result'])}; exit={occurrence['exit_code']}, ожидался {occurrence['expected_exit_code']}"
                )
            output = occurrences[0].get("output")
            if output:
                excerpt = output[:500]
                lines.append("    Совпавшая диагностика:")
                lines.extend(f"      {line}" for line in excerpt.splitlines())
                if len(output) > len(excerpt):
                    lines.append("      … полный сохранённый фрагмент есть в JSON и исходном журнале")
            actions = [action for interval in finding["between"] for action in interval["actions"]]
            paths = sorted({change["path"] for interval in finding["between"] for change in interval["changes"]})
            action_text = ", ".join(f"{action['tool']} (seq={action['seq']})" for action in actions[:4])
            if len(actions) > 4:
                action_text += f", … ещё {len(actions) - 4}; подробности — в JSON"
            lines.append(f"    Между попытками: {action_text or 'других инструментальных попыток не записано'}.")
            lines.append(f"    Изменения файлов: {', '.join(paths) or 'изменений в журнале не записано'}.")
            lines.append(f"    Интерпретация: {finding['interpretation']}")
        for warning in session["warnings"]:
            message = warning["message"] if isinstance(warning, dict) else str(warning)
            lines.append(f"  Предупреждение: {message}")
    for error in report["errors"]:
        lines.extend(["", f"Ошибка: {error['source']}: {error['message']}"])
    lines.extend(["", "Совпадение ошибок не устанавливает их причину или бесполезность повторного запуска."])
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    # JSON и перенаправленные отчёты имеют одинаковую UTF-8 кодировку на Windows/POSIX.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
    parameters = sys.argv[1:] if argv is None else argv
    try:
        args = build_parser().parse_args(parameters)
    except SystemExit as stopped:
        if stopped.code == 0 or "--json" not in parameters:
            raise
        from agentlens.analysis import METHOD, QUESTION

        report = {
            "schema_version": 1, "question": QUESTION, "method": METHOD,
            "input": None, "complete": False,
            "summary": {key: 0 for key in (
                "files_seen", "sessions_analyzed", "sessions_with_repeats", "executions",
                "failed_executions", "unknown_executions", "uncomparable_failures",
                "repeat_episodes", "repeated_failures", "incomplete_sessions",
            )},
            "sessions": [],
            "errors": [{"source": "CLI", "message": "Некорректные параметры команды; подробности в stderr и analyze --help."}],
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2
    from agentlens import analyze_path

    try:
        report = analyze_path(args.input)
    except KeyboardInterrupt:
        print("Анализ прерван пользователем.", file=sys.stderr)
        return 130
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        print(render_text(report), end="")
    for error in report["errors"]:
        print(f"agentlens: {error['source']}: {error['message']}", file=sys.stderr)
    return 2 if report["errors"] else 0
