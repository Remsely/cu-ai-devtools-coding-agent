"""AgentLens: офлайн-анализ журналов учебного coding agent."""

from __future__ import annotations

from pathlib import Path


def analyze_path(path: str | Path) -> dict:
    """Анализирует файл JSONL или каталог; ошибки возвращает в отчёте."""
    from agentlens.analysis import analyze_path as analyze

    return analyze(path)


__all__ = ["analyze_path"]
