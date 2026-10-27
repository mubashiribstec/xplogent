"""OpenAI-compatible provider.

Works with the OpenAI Chat Completions API and any compatible endpoint
(OpenRouter, local servers, etc.). Uses httpx directly so the official SDK is
optional. Supports streaming, tool calling, and embeddings.
"""

from __future__ import annotations

import json
import os
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
    is_reasoning_effort,
)


class OpenAIProvider(Provider):
    name = "openai"
    default_base_url = "https://api.openai.com/v1"
    api_key_env = "OPENAI_API_KEY"

    def _is_reasoning_model(self) -> bool:
        m = self.model.lower()
        return m.startswith(("o1", "o3", "o4")) or "gpt-5" in m or "reason" in m

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(model, **kwargs)
        self.base_url = (base_url or self.default_base_url).rstrip("/")
        self.api_key = api_key or os.environ.get(self.api_key_env, "")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        self._extra_headers(headers)
        self._client = httpx.AsyncClient(
            base_url=self.base_url, headers=headers, timeout=httpx.Timeout(300.0)
        )

    def _extra_headers(self, headers: dict[str, str]) -> None:
        """Hook for subclasses (e.g. OpenRouter) to add headers."""

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        temperature: float = 0.7,
        **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]:
        gen = extract_gen_params(kwargs)
        reasoning = self._is_reasoning_model()
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [m.to_openai() for m in messages],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        # Reasoning models (o-series / gpt-5) reject a custom temperature.
        if not reasoning:
            payload["temperature"] = temperature
        if gen["max_tokens"]:
            payload["max_completion_tokens" if reasoning else "max_tokens"] = gen["max_tokens"]
        # Reasoning effort only applies to reasoning models; sending it to
        # others can be rejected, so guard by model name.
        if is_reasoning_effort(gen["effort"]) and reasoning:
            payload["reasoning_effort"] = gen["effort"]
        if tools:
            payload["tools"] = [t.to_openai() for t in tools]

        content_parts: list[str] = []
        # tool call deltas accumulate by index
        tc_acc: dict[int, dict[str, Any]] = {}
        usage: dict[str, int] | None = None

        async with self._client.stream("POST", "/chat/completions", json=payload) as resp:
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError:
                await _raise_with_reason(resp)
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                if err := chunk.get("error"):
                    # Mid-stream error payload (OpenAI-compatible gateways send
                    # these): raise like the Anthropic provider does, so retry /
                    # backup applies instead of an empty "successful" answer.
                    msg = err.get("message", "") if isinstance(err, dict) else str(err)
                    exc = RuntimeError(f"openai stream error: {msg}")
                    exc.details = {"mid_stream": True, **(error_details(err) if isinstance(err, dict) else {})}
                    raise exc
                if u := chunk.get("usage"):
                    usage = {"input_tokens": int(u.get("prompt_tokens", 0)),
                             "output_tokens": int(u.get("completion_tokens", 0))}
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                if text := delta.get("content"):
                    content_parts.append(text)
                    yield StreamEvent(kind=StreamKind.TOKEN, text=text)
                for tc in delta.get("tool_calls", []) or []:
                    idx = tc.get("index", 0)
                    slot = tc_acc.setdefault(idx, {"id": "", "name": "", "args": ""})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if fn.get("arguments"):
                        # Some compatible endpoints send arguments as an object,
                        # not a JSON string; += would TypeError on a dict.
                        slot["args"] += fn["arguments"] if isinstance(
                            fn["arguments"], str) else json.dumps(fn["arguments"])

        tool_calls = []
        for slot in tc_acc.values():
            try:
                args = json.loads(slot["args"]) if slot["args"] else {}
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(ToolCall(id=slot["id"] or "call_0", name=slot["name"], arguments=args))

        final = Message(role=Role.ASSISTANT, content="".join(content_parts),
                        tool_calls=tool_calls, usage=usage)
        yield StreamEvent(kind=StreamKind.DONE, message=final)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        resp = await self._client.post(
            "/embeddings", json={"model": self.model, "input": texts}
        )
        resp.raise_for_status()
        # The API contract is the per-item `index`, not response order.
        data = sorted(resp.json()["data"], key=lambda i: i.get("index", 0))
        return [item["embedding"] for item in data]

    async def list_models(self) -> list[str]:
        resp = await self._client.get("/models")
        resp.raise_for_status()
        return [str(d["id"]) for d in resp.json().get("data", []) if d.get("id")]

    async def aclose(self) -> None:
        await self._client.aclose()


async def _raise_with_reason(resp: httpx.Response) -> None:
    """``raise_for_status`` with the provider's own reason (OpenRouter puts the useful
    part, e.g. "temporarily rate-limited upstream", in ``error.metadata.raw``), so
    retries / backup still see an ``HTTPStatusError`` and logs say what happened."""
    reason = ""
    details: dict[str, Any] = {"status": resp.status_code}
    try:
        err = json.loads(await resp.aread()).get("error") or {}
        if isinstance(err, dict):
            details.update(error_details(err))
            reason = str(details.get("raw") or details.get("message") or "")
        else:
            reason = str(err)
    except Exception:  # noqa: BLE001 - no readable body: the status line is enough
        pass
    msg = f"{resp.status_code} {resp.reason_phrase}" + (f": {reason[:300]}" if reason else "")
    exc = httpx.HTTPStatusError(msg, request=resp.request, response=resp)
    exc.details = details             # read by providers.diagnose for the reason + fix
    raise exc


def error_details(err: dict[str, Any]) -> dict[str, Any]:
    """The useful parts of an OpenAI-style / OpenRouter ``error`` object."""
    meta = err.get("metadata") if isinstance(err.get("metadata"), dict) else {}
    raw = meta.get("raw")
    if isinstance(raw, (dict, list)):
        raw = json.dumps(raw)[:400]
    return {k: v for k, v in {
        "code": err.get("code"), "message": str(err.get("message") or ""), "raw": str(raw or ""),
        "provider": str(meta.get("provider_name") or ""), "limit_source": str(meta.get("limit_source") or ""),
    }.items() if v not in ("", None)}
