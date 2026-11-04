"""Реестр инструментов: определение с JSON Schema, валидация, исполнение.

Ошибка инструмента указывает последний достигнутый слой (неделя 6):
разбор ответа → схема → реестр → политика → handler → упаковка результата.
Слои до handler не запускают код инструмента.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# Коды ошибок протокола.
PARSE_ERROR = "PARSE_ERROR"                    # слой разбора ответа модели
INVALID_ARGUMENTS = "INVALID_ARGUMENTS"        # слой схемы аргументов
UNKNOWN_TOOL = "UNKNOWN_TOOL"                  # слой реестра
DENIED_BY_POLICY = "DENIED_BY_POLICY"          # слой политики (фаза, пути)
BUDGET_EXCEEDED = "BUDGET_EXCEEDED"            # слой бюджета
PROTOCOL_ERROR = "PROTOCOL_ERROR"              # повтор tool_call_id с другими аргументами
HANDLER_ERROR = "HANDLER_ERROR"                # слой handler: непредвиденное исключение
FILE_NOT_FOUND = "FILE_NOT_FOUND"              # слой handler: ожидаемая ошибка среды
PATH_OUTSIDE_WORKSPACE = "PATH_OUTSIDE_WORKSPACE"
TIMEOUT = "TIMEOUT"
UNDEFINED_SKIPPED = "UNDEFINED_SKIPPED"
NOT_EXECUTED_AFTER_CRASH = "NOT_EXECUTED_AFTER_CRASH"
# Уровень 9: задачи.
TASK_DEPENDENCIES_NOT_MET = "TASK_DEPENDENCIES_NOT_MET"
VERIFICATION_REQUIRED = "VERIFICATION_REQUIRED"
VERIFICATION_FAILED = "VERIFICATION_FAILED"
# Уровень 10: дерево проекта изменено извне.
TREE_MODIFIED_EXTERNALLY = "TREE_MODIFIED_EXTERNALLY"


class ToolError(Exception):
    """Ожидаемая ошибка инструмента: не роняет цикл, возвращается модели."""

    def __init__(
        self,
        code: str,
        message: str,
        recoverable: bool = True,
        data: dict | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.recoverable = recoverable
        self.data = data

    def envelope(self) -> dict:
        error = {
            "code": self.code,
            "message": self.message,
            "recoverable": self.recoverable,
        }
        if self.data is not None:
            error["data"] = self.data
        return {"ok": False, "error": error}


_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}


@dataclass(frozen=True)
class Param:
    name: str
    type: str = "string"
    description: str = ""
    required: bool = True


@dataclass(frozen=True)
class ToolContext:
    workspace: Path
    max_output_bytes: int
    bash_timeout: float
    bash_path: str
    protected_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    params: tuple[Param, ...]
    run_fn: Callable[[dict, ToolContext], dict]
    side_effect: bool = False
    # Обоснование повторяемости после сбоя (блок 2) — см. README.
    safely_repeatable: bool = True

    def definition(self) -> dict:
        """Описание для модели: имя, описание и JSON Schema аргументов."""
        properties = {
            p.name: {"type": p.type, "description": p.description}
            for p in self.params
        }
        required = [p.name for p in self.params if p.required]
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            },
        }

    def validate(self, args) -> str | None:
        """Проверка имени и аргументов ДО выполнения. None — ок, иначе сообщение."""
        if not isinstance(args, dict):
            return f"аргументы должны быть JSON-объектом, получено {type(args).__name__}"
        known = {p.name for p in self.params}
        for key in args:
            if key not in known:
                return f"неизвестный аргумент '{key}' (additionalProperties: false)"
        for p in self.params:
            if p.required and p.name not in args:
                return f"отсутствует обязательный аргумент '{p.name}'"
            if p.name in args:
                check = _TYPE_CHECKS.get(p.type)
                if check and not check(args[p.name]):
                    return (
                        f"аргумент '{p.name}' должен иметь тип {p.type}, "
                        f"получено {type(args[p.name]).__name__}"
                    )
        return None


def execute(tool: Tool, args: dict, ctx: ToolContext) -> dict:
    """Исполнение handler с конвертацией исключений в error-конверт.

    Ошибка инструмента не роняет процесс: модель получает результат
    с кодом и сообщением и может выбрать другой шаг.
    """
    try:
        data = tool.run_fn(args, ctx)
        return {"ok": True, "data": data}
    except ToolError as err:
        return err.envelope()
    except Exception as err:  # noqa: BLE001 — контракт цикла: не падать
        return ToolError(HANDLER_ERROR, f"{type(err).__name__}: {err}").envelope()
