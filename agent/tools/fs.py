"""Инструменты файловой системы: list_files, read_file, write_file."""

from __future__ import annotations

from pathlib import Path

from agent.tools.base import (
    DENIED_BY_POLICY,
    FILE_NOT_FOUND,
    INVALID_ARGUMENTS,
    PATH_OUTSIDE_WORKSPACE,
    Param,
    Tool,
    ToolContext,
    ToolError,
)

SKIP_DIRS = {".git", "__pycache__", "runs", ".pytest_cache", ".idea", ".venv"}
SKIP_SUFFIXES = (".pyc", ".pyo")
TRUNCATION_NOTE = "\n...[output truncated]"


def safe_path(ctx: ToolContext, rel: str) -> Path:
    """Нормализация пути до проверки (неделя 8): resolve() + is_relative_to.

    Сравнение строковых префиксов не используется: '../x' и символические
    ссылки внутри workspace не выводят запись за его пределы.
    """
    if not isinstance(rel, str) or not rel.strip():
        raise ToolError(INVALID_ARGUMENTS, "путь должен быть непустой строкой")
    root = ctx.workspace.resolve()
    target = (root / rel).resolve()
    if target != root and not target.is_relative_to(root):
        raise ToolError(PATH_OUTSIDE_WORKSPACE, f"путь '{rel}' вне workspace")
    return target


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _list_files(args: dict, ctx: ToolContext) -> dict:
    root = ctx.workspace.resolve()
    start = safe_path(ctx, args.get("path", "."))
    files: list[str] = []
    for path in sorted(root.rglob("*")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix in SKIP_SUFFIXES or not path.is_file():
            continue
        if start == root or path.is_relative_to(start):
            files.append(_rel(root, path))
    return {"path": args.get("path", "."), "files": files}


def _read_file(args: dict, ctx: ToolContext) -> dict:
    target = safe_path(ctx, args["path"])
    if not target.exists() or not target.is_file():
        raise ToolError(FILE_NOT_FOUND, f"файл не найден: {args['path']}")
    raw = target.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    truncated = False
    cap = ctx.max_output_bytes
    if len(text) > cap:
        text = text[: max(0, cap - len(TRUNCATION_NOTE))] + TRUNCATION_NOTE
        truncated = True
    return {
        "path": _rel(ctx.workspace, target),
        "content": text,
        "truncated": truncated,
        "size_bytes": len(raw),
    }


def _write_file(args: dict, ctx: ToolContext) -> dict:
    target = safe_path(ctx, args["path"])
    rel = _rel(ctx.workspace, target)
    # Правило + механизм его исполняющий: защищённые пути (тесты контрольного
    # проекта) не перезаписываются; наблюдение — сверка хеша в тестах.
    for protected in ctx.protected_paths:
        if rel == protected or rel.startswith(protected.rstrip("/") + "/"):
            raise ToolError(
                DENIED_BY_POLICY,
                f"запись в '{rel}' запрещена политикой (защищённый путь)",
            )
    content = args["content"]
    if target.exists() and target.read_text(encoding="utf-8", errors="replace") == content:
        # Идемпотентность: повторная запись того же содержимого не меняет файл.
        return {"path": rel, "bytes_written": 0, "already_applied": True}
    target.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n": содержимое файла байт-в-байт равно content.encode()
    # (иначе перевод строк ОС ломает хеши снапшотов уровня 10)
    target.write_text(content, encoding="utf-8", newline="\n")
    return {"path": rel, "bytes_written": len(content.encode("utf-8")), "already_applied": False}


LIST_FILES = Tool(
    name="list_files",
    description=(
        "Возвращает отсортированный список файлов workspace (относительные пути). "
        "Ничего не изменяет. Служебные каталоги (.git, __pycache__, runs) пропускаются."
    ),
    params=(Param("path", "string", "Подкаталог относительно корня workspace; по умолчанию '.'", required=False),),
    run_fn=_list_files,
    side_effect=False,
    safely_repeatable=True,
)

READ_FILE = Tool(
    name="read_file",
    description="Читает текстовый файл по относительному пути в workspace. Ничего не изменяет.",
    params=(Param("path", "string", "Путь к файлу относительно корня workspace"),),
    run_fn=_read_file,
    side_effect=False,
    safely_repeatable=True,
)

WRITE_FILE = Tool(
    name="write_file",
    description=(
        "Полностью записывает текстовый файл по относительному пути в workspace "
        "(перезапись существующего). Для точечной правки сначала прочитай файл "
        "и передай новое полное содержимое."
    ),
    params=(
        Param("path", "string", "Путь к файлу относительно корня workspace"),
        Param("content", "string", "Полное новое содержимое файла"),
    ),
    run_fn=_write_file,
    side_effect=True,
    # Полная перезапись детерминирована: те же аргументы дают то же состояние,
    # а повторная запись того же содержимого не изменяет файл (already_applied).
    safely_repeatable=True,
)
