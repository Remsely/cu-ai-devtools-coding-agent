"""Инструмент Bash: команда + рабочая директория → stdout, stderr, exit code.

Обязательный инструмент задания. Границы исполнения (неделя 8):
- лимит времени: при таймауте завершается вся группа процессов, а не только
  shell (start_new_session + killpg на POSIX, taskkill /T на Windows);
- лимит размера вывода: обрезка с явной пометкой truncated;
- чистое окружение: дочернему процессу передаётся только allowlist
  переменных, секреты (например, OPENAI_API_KEY) не наследуются.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time

from agent.tools.base import (
    FILE_NOT_FOUND,
    INVALID_ARGUMENTS,
    TIMEOUT,
    Param,
    Tool,
    ToolContext,
    ToolError,
)
from agent.tools.fs import safe_path

ENV_ALLOW = {
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    # Windows: без системных переменных bash и python в нём не стартуют
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "COMSPEC",
    "WINDIR",
    "USERPROFILE",
    "TMP",
    "TEMP",
    "PYTHONIOENCODING",
    "PYTHONUTF8",
}

TRUNCATION_NOTE = "\n...[output truncated]"


def child_env() -> dict[str, str]:
    return {k: os.environ[k] for k in ENV_ALLOW if k in os.environ}


def _kill_tree(pid: int) -> None:
    if os.name == "posix":
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    keep = max(0, limit - len(TRUNCATION_NOTE))
    return text[:keep] + TRUNCATION_NOTE, True


def run_bash(args: dict, ctx: ToolContext) -> dict:
    command = args["command"]
    if not isinstance(command, str) or not command.strip():
        raise ToolError(INVALID_ARGUMENTS, "команда должна быть непустой строкой")
    cwd = safe_path(ctx, args.get("cwd", "."))
    if not cwd.is_dir():
        raise ToolError(FILE_NOT_FOUND, f"рабочая директория не найдена: {args.get('cwd', '.')}")

    popen_kwargs: dict = {}
    creationflags = 0
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True  # своя группа процессов
    else:
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

    started = time.monotonic()
    proc = subprocess.Popen(
        [ctx.bash_path, "-c", command],
        cwd=str(cwd),
        env=child_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
        **popen_kwargs,
    )
    timed_out = False
    try:
        out, err = proc.communicate(timeout=ctx.bash_timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc.pid)
        try:
            out, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            _kill_tree(proc.pid)
            out, err = proc.communicate()
    duration = round(time.monotonic() - started, 3)

    out = out or ""
    err = err or ""
    cap = ctx.max_output_bytes
    out, out_cut = _truncate(out, cap)
    err, err_cut = _truncate(err, max(0, cap - len(out)))  # stdout приоритетнее

    payload = {
        "command": command,
        "cwd": args.get("cwd", "."),
        "exit_code": None if timed_out else proc.returncode,
        "timed_out": timed_out,
        "duration_s": duration,
        "stdout": out,
        "stderr": err,
        "truncated": out_cut or err_cut,
    }
    if timed_out:
        raise ToolError(
            TIMEOUT,
            f"команда превысила лимит времени {ctx.bash_timeout} c и была завершена",
            recoverable=True,
            data=payload,
        )
    # Ненулевой код выхода — это наблюдение, а не ошибка инструмента:
    # команда отработала, модель читает stderr и решает следующий шаг.
    return payload


BASH = Tool(
    name="Bash",
    description=(
        "Запускает команду через Bash в рабочей директории внутри workspace "
        "и возвращает stdout, stderr и код завершения. Есть лимит времени "
        "и лимит размера вывода (лишнее обрезается с пометкой truncated). "
        "Используй для запуска тестов, например: python -m unittest -v"
    ),
    params=(
        Param("command", "string", "Команда Bash для выполнения"),
        Param("cwd", "string", "Рабочая директория относительно корня workspace (по умолчанию '.')", required=False),
    ),
    run_fn=run_bash,
    side_effect=True,
    # Команда могла изменить файлы или внешнее состояние — повтор только
    # по явному решению пользователя (блок 2).
    safely_repeatable=False,
)
