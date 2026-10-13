"""Provider abstraction.

Every LLM provider — local Ollama, OpenAI, Anthropic, OpenRouter — implements
the same small interface so the agent loop is provider-agnostic. Messages and
tool calls are normalized to an OpenAI-style shape internally; each provider
translates to/from its native wire format.
"""

from __future__ import annotations

import base64
import json
import mimetypes
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


def image_data_uri(path_or_uri: str) -> tuple[str, str]:
    """Return ``(media_type, base64_data)`` for a local image path or data URI.

    Accepts an existing ``data:`` URI (passed through) or a filesystem path,
    which is read and base64-encoded. Used to feed images to vision-capable models.
    """
    if path_or_uri.startswith("data:"):
        header, _, b64 = path_or_uri.partition(",")
        media = header[len("data:"):].split(";", 1)[0] or "image/png"
        return media, b64
    p = Path(path_or_uri).expanduser()
    media = mimetypes.guess_type(p.name)[0] or "image/png"
    return media, base64.b64encode(p.read_bytes()).decode("ascii")


@dataclass
class ToolCall:
    """A model request to invoke a tool."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class Message:
    """A single conversation message in normalized form."""

    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None  # set on TOOL messages
    name: str | None = None          # tool name on TOOL messages
    images: list[str] = field(default_factory=list)  # image paths/data-URIs (vision)
    usage: dict[str, int] | None = None  # {input_tokens, output_tokens} when reported
    # Provider-native reasoning blocks that must be echoed back verbatim on the next
    # request (Anthropic extended thinking + tool use requires the signed blocks).
    thinking_blocks: list[dict[str, Any]] = field(default_factory=list)

    def to_openai(self) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": self.role.value}
        if self.role == Role.TOOL:
            msg["content"] = self.content
            msg["tool_call_id"] = self.tool_call_id or ""
            return msg
        # Multimodal: a USER message carrying images becomes a content-parts list.
        if self.images and self.role == Role.USER:
            parts: list[dict[str, Any]] = []
            if self.content:
                parts.append({"type": "text", "text": self.content})
            for img in self.images:
                media, b64 = image_data_uri(img)
                parts.append({"type": "image_url",
                              "image_url": {"url": f"data:{media};base64,{b64}"}})
            msg["content"] = parts
            return msg
        msg["content"] = self.content or ""
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                }
                for tc in self.tool_calls
            ]
        return msg


@dataclass
class ToolSpec:
    """A tool definition exposed to the model for function calling."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema

    def to_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class StreamKind(StrEnum):
    TOKEN = "token"      # a content delta
    DONE = "done"        # final assembled Message available


@dataclass
class StreamEvent:
    kind: StreamKind
    text: str = ""
    message: Message | None = None


# Reasoning "effort" → approximate thinking budget in tokens (Anthropic/Ollama).
EFFORT_BUDGET = {"low": 1024, "medium": 4096, "high": 10000}
_REASONING_EFFORTS = {"low", "medium", "high"}


def extract_gen_params(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Pull the normalized generation controls out of provider kwargs.

    Returns ``{effort, thinking, max_tokens}``; ``effort`` is one of
    off/low/medium/high (or None), ``thinking`` a bool, ``max_tokens`` an int.
    """
    effort = kwargs.pop("effort", None)
    if effort in (None, "off", ""):
        effort = None
    thinking = bool(kwargs.pop("thinking", False))
    max_tokens = kwargs.pop("max_tokens", None)
    return {"effort": effort, "thinking": thinking, "max_tokens": max_tokens}


def is_reasoning_effort(effort: Any) -> bool:
    return effort in _REASONING_EFFORTS


class Provider(ABC):
    """Abstract LLM provider."""

    name: str = "base"

    def __init__(self, model: str, **kwargs: Any) -> None:
        self.model = model
        self.options = kwargs

    @abstractmethod
    def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        temperature: float = 0.7,
        **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]:
        """Stream a completion, yielding TOKEN events then a final DONE event."""
        raise NotImplementedError

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        **kwargs: Any,
    ) -> Message:
        """Non-streaming convenience wrapper that returns the final message."""
        final: Message | None = None
        async for event in self.stream(messages, tools, **kwargs):
            if event.kind == StreamKind.DONE:
                final = event.message
        return final or Message(role=Role.ASSISTANT, content="")

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Return embeddings for ``texts``. Override in providers that support it."""
        raise NotImplementedError(f"{self.name} does not support embeddings")

    async def list_models(self) -> list[str]:
        """Return the provider's available model ids. Override per provider."""
        return []

    async def aclose(self) -> None:  # pragma: no cover - cleanup hook
        return None
