"""Клиенты моделей: ScriptedModel (детерминированная) и OpenAI-compatible.

Единый интерфейс chat(messages, tools) -> ModelReply. Клиент — только
транспорт: он не выбирает инструменты и не принимает решений.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field


@dataclass
class ToolCallRequest:
    id: str
    name: str
    arguments: dict | None  # None, если аргументы не удалось разобрать
    parse_error: str | None = None


@dataclass
class ModelReply:
    text: str | None = None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    raw: dict | None = None


class ScriptExhausted(RuntimeError):
    """Сценарий scripted-модели закончился раньше конца запуска."""


class LLMAPIError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def canonical_arguments(args: dict) -> str:
    """Канонический JSON аргументов: один и тот же dict всегда даёт одну строку."""
    return json.dumps(args, ensure_ascii=False, sort_keys=True)


def assistant_message(reply: ModelReply) -> dict:
    """Каноническое assistant-сообщение в формате OpenAI Chat Completions."""
    message: dict = {"role": "assistant", "content": reply.text}
    if reply.tool_calls:
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": canonical_arguments(call.arguments or {}),
                },
            }
            for call in reply.tool_calls
        ]
    return message


class ScriptedModel:
    """Детерминированная модель для тестов и демонстраций без сети.

    Сценарий — список реплик: {"text": "..."} или
    {"tool_calls": [{"id": ..., "name": ..., "arguments": {...}}]}.
    Реплики выдаются по одной на обращение, что делает тесты runtime
    полностью воспроизводимыми (требование задания к детерминированным тестам).
    """

    def __init__(self, script: list[dict]):
        self.script = list(script)
        self._pos = 0
        self.requests_seen: list[dict] = []

    def chat(self, messages: list[dict], tools: list[dict]) -> ModelReply:
        self.requests_seen.append(
            {
                "tools": [t["function"]["name"] for t in tools],
                "messages": len(messages),
            }
        )
        if self._pos >= len(self.script):
            raise ScriptExhausted(
                f"сценарий исчерпан (использовано {self._pos} реплик из {len(self.script)})"
            )
        item = self.script[self._pos]
        self._pos += 1
        reply = ModelReply(raw=item)
        if "text" in item:
            reply.text = item["text"]
            return reply
        for call in item.get("tool_calls", []):
            reply.tool_calls.append(
                ToolCallRequest(
                    id=call["id"],
                    name=call["name"],
                    arguments=call.get("arguments", {}),
                )
            )
        return reply


class OpenAICompatModel:
    """Тонкий клиент OpenAI-compatible Chat Completions (только stdlib).

    Работает с любым base_url: облачный провайдер или локальная Ollama
    (OPENAI_BASE_URL=http://localhost:11434/v1/, ключ — любая непустая строка).
    """

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 300.0,
                 extra_payload: dict | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        # Доп. поля запроса (например, {"think": false} для Ollama/qwen3.x).
        self.extra_payload = extra_payload or {}

    @classmethod
    def from_env(cls) -> "OpenAICompatModel":
        base_url = os.environ.get("OPENAI_BASE_URL", "").strip()
        api_key = os.environ.get("OPENAI_API_KEY", "").strip() or "EMPTY"
        model = os.environ.get("OPENAI_MODEL", "").strip()
        if not base_url or not model:
            raise LLMAPIError(
                "задайте OPENAI_BASE_URL и OPENAI_MODEL (пример — в .env.example)"
            )
        try:
            extra = json.loads(os.environ.get("AGENT_LLM_EXTRA", "") or "{}")
        except ValueError as err:
            raise LLMAPIError(f"AGENT_LLM_EXTRA не является JSON: {err}") from err
        return cls(base_url, api_key, model, extra_payload=extra)

    def chat(self, messages: list[dict], tools: list[dict]) -> ModelReply:
        payload = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            **self.extra_payload,
        }
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            detail = err.read().decode("utf-8", errors="replace")[:500]
            raise LLMAPIError(f"HTTP {err.code} от модели: {detail}", status=err.code) from err
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            raise LLMAPIError(f"модель недоступна: {err}") from err

        choice = body["choices"][0]["message"]
        reply = ModelReply(raw=body)
        reply.text = choice.get("content")
        for call in choice.get("tool_calls") or []:
            fn = call.get("function", {})
            raw_args = fn.get("arguments") or "{}"
            arguments: dict | None
            parse_error: str | None = None
            try:
                arguments = json.loads(raw_args)
                if not isinstance(arguments, dict):
                    raise ValueError("не JSON-объект")
            except ValueError as err:
                arguments, parse_error = None, f"аргументы не разобраны: {err}"
            reply.tool_calls.append(
                ToolCallRequest(
                    id=call.get("id", ""),
                    name=fn.get("name", ""),
                    arguments=arguments,
                    parse_error=parse_error,
                )
            )
        return reply
