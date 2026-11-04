"""Счётчики и проверки единого бюджета запуска (уровень 8).

Бюджет привязан к run_id, а не к процессу: счётчики выводятся replay-ем
журнала, поэтому resume их не обнуляет. Списание происходит ДО действия
(событие budget_debited / tool_call_started), и после падения не возвращается.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent.config import Limits


@dataclass
class Counters:
    model_requests: int = 0
    tool_calls: int = 0
    side_effects: int = 0
    output_bytes: int = 0

    def as_dict(self) -> dict:
        return {
            "model_requests": self.model_requests,
            "tool_calls": self.tool_calls,
            "side_effects": self.side_effects,
            "output_bytes": self.output_bytes,
        }


def model_request_allowed(counters: Counters, limits: Limits) -> bool:
    return counters.model_requests < limits.model_requests


def batch_refusal_reason(
    counters: Counters, limits: Limits, side_effect_flags: list[bool]
) -> str | None:
    """Проверка бюджета на всю пачку tool calls сразу.

    Пограничный случай №1 из задания: если остатка не хватает на всю пачку,
    не выполняется ни один вызов. Возвращает имя исчерпанного лимита
    или None, если пачка помещается.
    """
    if counters.tool_calls + len(side_effect_flags) > limits.tool_calls:
        return "tool_calls"
    if counters.side_effects + sum(side_effect_flags) > limits.side_effects:
        return "side_effects"
    if counters.output_bytes >= limits.output_bytes:
        # Размер результата заранее неизвестен: если остаток уже нулевой,
        # ни один вызов пачки не сможет вернуть результат.
        return "output_bytes"
    return None


def output_cap(counters: Counters, limits: Limits) -> int:
    """Доступный остаток вывода для одного инструмента.

    Пограничный случай №2: инструмент получает остаток, runtime обрезает
    вывод и помечает его неполным. Если остаток нулевой — вызов не начинается.
    """
    return limits.output_bytes - counters.output_bytes
