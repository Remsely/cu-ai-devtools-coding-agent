"""Конфигурация запуска: окружение, лимиты бюджета, пути."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path


@dataclass(frozen=True)
class Limits:
    """Единый бюджет запуска (уровень 8).

    Единицы счёта зафиксированы (см. README):
    - model_requests: одно обращение к Chat Completions (model turn);
    - tool_calls: исполненные вызовы инструментов (после валидации);
    - side_effects: вызовы инструментов с side_effect=True (включая Bash);
    - output_bytes: суммарный размер результатов инструментов (байты JSON).
    """

    model_requests: int = 40
    tool_calls: int = 80
    side_effects: int = 30
    output_bytes: int = 262_144

    @classmethod
    def from_json(cls, raw: str | None) -> "Limits":
        limits = cls()
        if not raw:
            return limits
        data = json.loads(raw)
        unknown = set(data) - set(limits.__dict__)
        if unknown:
            raise ValueError(f"неизвестные лимиты: {sorted(unknown)}")
        return replace(limits, **{k: int(v) for k, v in data.items()})

    def as_dict(self) -> dict:
        return {
            "model_requests": self.model_requests,
            "tool_calls": self.tool_calls,
            "side_effects": self.side_effects,
            "output_bytes": self.output_bytes,
        }


@dataclass(frozen=True)
class Config:
    workspace: Path
    runs_dir: Path
    limits: Limits = field(default_factory=Limits)
    bash_timeout: float = 60.0
    bash_path: str = ""
    failpoint: str | None = None
    # Пути в workspace, защищённые от записи (правило + наблюдение: запрет
    # здесь, сверка хеша — в acceptance-тестах). Тесты контрольного проекта
    # агент менять не должен.
    protected_paths: tuple[str, ...] = ("test_order.py",)

    @classmethod
    def from_env(
        cls,
        workspace: Path,
        runs_dir: Path,
        limits: Limits | None = None,
        failpoint: str | None = None,
    ) -> "Config":
        bash_path = shutil.which("bash")
        if not bash_path:
            raise RuntimeError(
                "bash не найден в PATH. На Windows нужен Git Bash или WSL "
                "(см. README, раздел «Установка»)."
            )
        return cls(
            workspace=workspace.resolve(),
            runs_dir=runs_dir.resolve(),
            limits=limits or Limits(),
            bash_timeout=float(os.environ.get("AGENT_BASH_TIMEOUT", "60")),
            bash_path=bash_path,
            failpoint=failpoint or os.environ.get("AGENT_FAILPOINT"),
        )

    @property
    def run_root(self) -> Path:
        return self.runs_dir
