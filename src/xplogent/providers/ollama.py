"""Local Ollama provider (native API).

Talks to a local Ollama server (default ``http://localhost:11434``). Supports
streaming chat with tool calling and embeddings — so Xplogent runs fully offline.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx

from xplogent.providers.base import (
    Message,
    Provider,
    Role,
    StreamEvent,
    StreamKind,
    ToolCall,
    ToolSpec,
    extract_gen_params,
    image_data_uri,
)


def _to_ollama(messages: list[Message]) -> list[dict[str, Any]]:
    """OpenAI-shaped messages, plus Ollama's native ``images`` (base64) field."""
    out: list[dict[str, Any]] = []
    for m in messages:
        payload = m.to_openai()
        # OpenAI wants tool-call arguments as a JSON string; Ollama wants an object and
        # rejects the string with 400 ("Value looks like object…") on the next turn.
        for tc in payload.get("tool_calls") or []:
            fn = tc.get("function") or {}
            if isinstance(fn.get("arguments"), str):
                try:
                    fn["arguments"] = json.loads(fn["arguments"] or "{}")
                except json.JSONDecodeError:
                    fn["arguments"] = {}
        if m.images and m.role == Role.USER:
            # Ollama wants plain text content + a separate base64 images list.
            payload["content"] = m.content or ""
            payload["images"] = [image_data_uri(img)[1] for img in m.images]
        out.append(payload)
    return out


DEFAULT_HOST = "http://localhost:11434"


def ollama_settings() -> tuple[str, str]:
    """``(host, bearer_token)`` for the Ollama server.

    The host set in the dashboard (config ``ollama.host``) wins, then ``OLLAMA_HOST``,
    then localhost. The token (``OLLAMA_API_KEY``, kept in the encrypted key store) is
    sent as ``Authorization: Bearer`` for servers behind an authenticating proxy.
    """
    host = ""
    try:
        from xplogent.core.config import load_config

        host = str((load_config().raw.get("ollama") or {}).get("host") or "").strip()
    except Exception:  # noqa: BLE001 - fall back to the environment
        host = ""
    host = host or os.environ.get("OLLAMA_HOST", "").strip() or DEFAULT_HOST
    return host.rstrip("/"), os.environ.get("OLLAMA_API_KEY", "").strip()


def server_token_env(name: str) -> str:
    """Secret name holding a named server's bearer token: ``OLLAMA_API_KEY_<NAME>``."""
    return "OLLAMA_API_KEY_" + re.sub(r"[^A-Z0-9]", "_", name.strip().upper())[:32]


def ollama_servers() -> list[dict[str, str]]:
    """Named Ollama servers from Settings (config ``ollama.servers``): ``[{name, host}]``."""
    try:
        from xplogent.core.config import load_config

        raw = (load_config().raw.get("ollama") or {}).get("servers") or []
    except Exception:  # noqa: BLE001
        raw = []
    out = []
    for s in raw if isinstance(raw, list) else []:
        if isinstance(s, dict) and str(s.get("name") or "").strip() and str(s.get("host") or "").strip():
            out.append({"name": str(s["name"]).strip(), "host": str(s["host"]).strip().rstrip("/")})
    return out


def ollama_server(name: str = "") -> tuple[str, str]:
    """``(host, token)`` for a named server, or the default server when ``name`` is empty."""
    if not name or name == "default":
        return ollama_settings()
    for s in ollama_servers():
        if s["name"] == name:
            return s["host"], os.environ.get(server_token_env(name), "").strip()
    raise ValueError(f"no Ollama server named '{name}': add it under Settings → Ollama servers")


def split_server(model: str) -> tuple[str, str]:
    """``qwen2.5:14b@gpu`` → ``("qwen2.5:14b", "gpu")`` (model names never contain ``@``)."""
    if "@" in model:
        base, _, server = model.rpartition("@")
        return base, server.strip()
    return model, ""


def ollama_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


class OllamaProvider(Provider):
    name = "ollama"

    def __init__(self, model: str, host: str | None = None, api_key: str | None = None,
                 **kwargs: Any) -> None:
        model, self.server = split_server(model)
        super().__init__(model, **kwargs)
        cfg_host, cfg_token = ollama_server(self.server)
        self.host = (host or cfg_host).rstrip("/")
        token = cfg_token if api_key is None else api_key
        self._client = httpx.AsyncClient(base_url=self.host, timeout=httpx.Timeout(300.0),
                                         headers=ollama_headers(token))

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        temperature: float = 0.7,
        **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]:
        gen = extract_gen_params(kwargs)
        options: dict[str, Any] = {"temperature": temperature}
        if gen["max_tokens"]:
            options["num_predict"] = gen["max_tokens"]
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": _to_ollama(messages),
            "stream": True,
            "options": options,
        }
        # Ollama "think" flag for thinking-capable models.
        if gen["thinking"] or gen["effort"] in ("medium", "high"):
            payload["think"] = True
        if tools:
            payload["tools"] = [t.to_openai() for t in tools]

        content_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        usage: dict[str, int] | None = None

        async with self._client.stream("POST", "/api/chat", json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                chunk = json.loads(line)
                if chunk.get("error"):
                    raise RuntimeError(f"ollama stream error: {chunk['error']}")
                if chunk.get("done") and ("eval_count" in chunk or "prompt_eval_count" in chunk):
                    usage = {"input_tokens": int(chunk.get("prompt_eval_count", 0)),
                             "output_tokens": int(chunk.get("eval_count", 0))}
                msg = chunk.get("message") or {}
                delta = msg.get("content") or ""
                if delta:
                    content_parts.append(delta)
                    yield StreamEvent(kind=StreamKind.TOKEN, text=delta)
                for tc in msg.get("tool_calls", []) or []:
                    fn = tc.get("function", {})
                    args = fn.get("arguments", {})
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {}
                    tool_calls.append(
                        ToolCall(id=tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                                 name=fn.get("name", ""), arguments=args)
                    )

        final = Message(role=Role.ASSISTANT, content="".join(content_parts),
                        tool_calls=tool_calls, usage=usage)
        yield StreamEvent(kind=StreamKind.DONE, message=final)

    async def list_models(self) -> list[str]:
        resp = await self._client.get("/api/tags")
        resp.raise_for_status()
        return [str(m["name"]) for m in resp.json().get("models", []) if m.get("name")]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            resp = await self._client.post(
                "/api/embeddings", json={"model": self.model, "prompt": text}
            )
            resp.raise_for_status()
            out.append(resp.json().get("embedding", []))
        return out

    async def aclose(self) -> None:
        await self._client.aclose()


LOCAL_DEFAULT_HOST = "http://ollama:11434"  # the `ollama` service in the Docker compose file


def local_ollama_host() -> str:
    """Host of the local (backup) Ollama: config ``fallback.host`` or OLLAMA_LOCAL_HOST."""
    host = ""
    try:
        from xplogent.core.config import load_config

        host = str((load_config().raw.get("fallback") or {}).get("host") or "").strip()
    except Exception:  # noqa: BLE001
        host = ""
    return (host or os.environ.get("OLLAMA_LOCAL_HOST", "").strip() or LOCAL_DEFAULT_HOST).rstrip("/")


class LocalOllamaProvider(OllamaProvider):
    """The Ollama running next to Xplogent (Docker ``ollama`` profile), used as the
    backup model. Spec ``ollama-local:<model>``; no token, independent of the main
    Ollama server configured in Settings."""

    name = "ollama-local"

    def __init__(self, model: str, host: str | None = None, **kwargs: Any) -> None:
        super().__init__(model, host=host or local_ollama_host(), api_key="", **kwargs)
