"""Уровень 10: журнал изменений, снапшоты дерева, diff и откат.

Механика (по заданию):
- перед каждым действием, которое может менять файлы проекта (write_file,
  Bash — у обоих side_effect=True), runtime сверяет текущее дерево
  с последним сохранённым состоянием и сохраняет снимок ДО действия
  (содержимое + список путей — фиксируются и создания, и удаления);
- расхождение с последним сохранённым состоянием = посторонняя правка:
  действие останавливается с TREE_MODIFIED_EXTERNALLY;
- после действия сохраняется фактический diff и хеши;
- откат восстанавливает только файлы, изменённые этим запуском, и не трогает
  остальные; перед откатом текущие хеши сверяются с журналом — при внешнем
  изменении хотя бы одного файла откат не выполняется вовсе;
- откат доступен только после разрешения неопределённых вызовов (блок 2);
  эффект неопределённого вызова относится к запуску решением пользователя
  (retry/skip) и фиксируется в журнале изменений с attribution.

Исключения для генерируемых файлов заданы явно и не скрывают исходники
или тесты контрольного проекта: __pycache__/, *.pyc/.pyo и служебные каталоги.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
from pathlib import Path

from agent import events

# Явные исключения: генерируемые и служебные артефакты.
IGNORED_DIRS = {"__pycache__", ".git", "runs", ".pytest_cache", ".idea", ".venv"}
IGNORED_SUFFIXES = (".pyc", ".pyo")
MAX_CONTENT_STORE = 512 * 1024  # содержимое длиннее не хранится в снимке


class ChangeConflict(RuntimeError):
    """Конфликт дерева: посторонние правки или неразрешённое состояние."""

    def __init__(self, message: str, paths: list[str] | None = None):
        super().__init__(message)
        self.paths = paths or []


def say(text: str) -> None:
    print(text, flush=True)


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _workspace_files(workspace: Path) -> list[Path]:
    root = workspace.resolve()
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if any(part in IGNORED_DIRS for part in path.parts):
            continue
        if path.suffix in IGNORED_SUFFIXES or not path.is_file():
            continue
        files.append(path)
    return files


def scan_tree(workspace: Path) -> dict[str, str]:
    """Снимок дерева: relpath -> sha256 (список путей фиксирует и удаления)."""
    root = workspace.resolve()
    return {
        path.relative_to(root).as_posix(): _hash_bytes(path.read_bytes())
        for path in _workspace_files(workspace)
    }


def _read_content(path: Path) -> str | None:
    data = path.read_bytes()
    if len(data) > MAX_CONTENT_STORE:
        return None  # слишком большой файл: хеш есть, содержимое не храним
    return data.decode("utf-8", errors="replace")


def _snapshot_dir(cfg, run_id: str) -> Path:
    return cfg.runs_dir / run_id / "snapshots"


def save_snapshot(state, journal, cfg) -> str:
    """Сохраняет снимок дерева (sidecar-файл) и событие snapshot_saved."""
    snapshot_id = f"snap{len(state.snapshots) + 1}"
    tree = scan_tree(cfg.workspace)
    contents: dict[str, dict] = {}
    for path in _workspace_files(cfg.workspace):
        rel = path.relative_to(cfg.workspace.resolve()).as_posix()
        contents[rel] = {"hash": tree[rel], "content": _read_content(path)}
    sidecar_dir = _snapshot_dir(cfg, state.run_id)
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    sidecar = sidecar_dir / f"{snapshot_id}.json"
    tmp = sidecar.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({"snapshot_id": snapshot_id, "tree": tree, "contents": contents},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(tmp, sidecar)  # атомарная замена
    journal.append(
        events.SNAPSHOT_SAVED,
        snapshot_id=snapshot_id,
        file=f"snapshots/{snapshot_id}.json",
        tree=tree,
    )
    return snapshot_id


def tree_diff(before: dict[str, str], after: dict[str, str]) -> list[dict]:
    entries = []
    for path in sorted(set(before) | set(after)):
        b, a = before.get(path), after.get(path)
        if b == a:
            continue
        change = "created" if b is None else "deleted" if a is None else "modified"
        entries.append(
            {"path": path, "change": change, "before_hash": b, "after_hash": a}
        )
    return entries


def conflicts(state, cfg) -> list[str]:
    """Пути, изменённые извне относительно последнего сохранённого состояния."""
    if state.last_tree is None:
        return []
    current = scan_tree(cfg.workspace)
    return [e["path"] for e in tree_diff(state.last_tree, current)]


def record_change(state, journal, cfg, call_id: str, attribution: str = "action") -> None:
    """Фиксирует фактический diff после побочного действия и новое дерево."""
    before = state.last_tree or {}
    after = scan_tree(cfg.workspace)
    entries = tree_diff(before, after)
    for entry in entries:
        if entry["change"] != "deleted":
            target = cfg.workspace / entry["path"]
            entry["after_content"] = _read_content(target)
        else:
            entry["after_content"] = None
    journal.append(
        events.CHANGE_RECORDED,
        call_id=call_id,
        attribution=attribution,
        diff=entries,
        after_tree=after,
    )
    if entries:
        summary = ", ".join(f"{e['change']} {e['path']}" for e in entries)
        say(f"[изменения] {call_id}: {summary}")


def absorb_undefined_effect(state, journal, cfg, call_id: str) -> bool:
    """Относит эффект неопределённого вызова к запуску (уровень 10 + блок 2).

    Вызывается при решении retry/skip: если дерево изменилось относительно
    последнего сохранённого состояния, изменения фиксируются в журнале
    с attribution='undefined_call'. Без этого retry упёрся бы в конфликт
    дерева, а откат не смог бы восстановить файл.
    """
    if state.last_tree is None:
        return False
    current = scan_tree(cfg.workspace)
    if current == state.last_tree:
        return False
    record_change(state, journal, cfg, call_id, attribution="undefined_call")
    say("[recovery] изменения дерева от неопределённого вызова отнесены к запуску")
    return True


def load_snapshot_contents(cfg, run_id: str, snapshot: dict) -> dict[str, dict]:
    sidecar = cfg.runs_dir / run_id / snapshot["file"]
    return json.loads(sidecar.read_text(encoding="utf-8"))["contents"]


def aggregate_changes(state) -> dict[str, dict]:
    """По файлу: исходное состояние до первого изменения и итоговое после
    последнего (before_hash — из первого изменения, остальное — из последнего)."""
    aggregated: dict[str, dict] = {}
    for change in state.changes:
        for entry in change.get("diff", []):
            path = entry["path"]
            if path not in aggregated:
                aggregated[path] = {"before_hash": entry["before_hash"]}
            info = aggregated[path]
            info["change"] = entry["change"]
            info["after_hash"] = entry["after_hash"]
            info["after_content"] = entry.get("after_content")
    return aggregated


def diff_report(state, cfg) -> list[str]:
    """Человекочитаемый отчёт: что изменил этот запуск (данные журнала)."""
    if not state.changes:
        return ["Изменений нет: побочных действий с эффектом не было."]
    aggregated = aggregate_changes(state)
    originals = (
        load_snapshot_contents(cfg, state.run_id, state.snapshots[0])
        if state.snapshots else {}
    )
    lines = [f"Изменения запуска {state.run_id} ({len(aggregated)} файл(ов)):"]
    for path in sorted(aggregated):
        info = aggregated[path]
        lines.append(f"  {path}: {info['change']} "
                     f"({info['before_hash'] or '—'} → {info['after_hash'] or '—'})")
        before = (originals.get(path) or {}).get("content")
        if info["change"] == "modified" and before is not None and info["after_content"] is not None:
            diff = difflib.unified_diff(
                before.splitlines(keepends=True),
                (info["after_content"] or "").splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
            )
            lines.extend("    " + line.rstrip("\n") for line in diff)
    return lines


def rollback(state, journal, cfg) -> dict:
    """Откат изменений конкретного запуска по явной команде пользователя."""
    undefined = state.undefined_calls()
    if undefined:
        raise ChangeConflict(
            "есть неопределённые после сбоя вызовы: "
            + ", ".join(rec.id for rec in undefined)
            + ". Сначала разрешите их: python -m agent resume " + state.run_id
        )
    if state.rolled_back:
        raise ChangeConflict("откат для этого запуска уже выполнялся")
    aggregated = aggregate_changes(state)
    if not aggregated:
        journal.append(events.ROLLBACK_PERFORMED, restored=[], removed=[])
        say("[откат] у этого запуска нет изменений файлов")
        return {"restored": [], "removed": [], "note": "изменений не было"}

    # Сверка: текущие хеши должны совпадать с итоговыми хешами журнала.
    current = scan_tree(cfg.workspace)
    mismatched = [
        path
        for path, info in aggregated.items()
        if current.get(path) != info["after_hash"]
    ]
    if mismatched:
        raise ChangeConflict(
            "файлы изменены извне после последнего действия агента, "
            "откат не выполняется: " + ", ".join(sorted(mismatched)),
            paths=mismatched,
        )

    # Восстанавливаем ТОЛЬКО файлы, затронутые этим запуском.
    originals = load_snapshot_contents(cfg, state.run_id, state.snapshots[0])
    restored: list[str] = []
    removed: list[str] = []
    for path in sorted(aggregated):
        target = cfg.workspace / path
        original = originals.get(path)
        if original is not None and original.get("content") is not None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(original["content"], encoding="utf-8", newline="\n")
            restored.append(path)
        elif target.exists():
            target.unlink()
            removed.append(path)
    journal.append(events.ROLLBACK_PERFORMED, restored=restored, removed=removed)
    say(f"[откат] восстановлены: {restored or '—'}; удалены созданные: {removed or '—'}")
    return {"restored": restored, "removed": removed}
