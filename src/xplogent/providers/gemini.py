"""Google Gemini provider (Generative Language API).

Translates the normalized OpenAI-style messages/tools into Gemini's
``contents``/``systemInstruction``/``functionDeclarations`` shape and streams
back via SSE. Uses httpx so the Google SDK is optional.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx

from xplogent.providers.base import (
    EFFORT_BUDGET,
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


class GeminiProvider(Provider):
    name = "gemini"

    def __init__(self, model: str, api_key: str | None = None,
                 base_url: str | None = None, **kwargs: Any) -> None:
        super().__init__(model, **kwargs)
        self.base_url = (base_url
                         or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        self.api_key = (api_key or os.environ.get("GOOGLE_API_KEY")
                        or os.environ.get("GEMINI_API_KEY", ""))
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
            timeout=httpx.Timeout(300.0),
        )

    def _convert(self, messages: list[Message]) -> tuple[dict | None, list[dict]]:
        system_parts: list[str] = []
        contents: list[dict[str, Any]] = []
        for m in messages:
            if m.role == Role.SYSTEM:
                system_parts.append(m.content)
            elif m.role == Role.TOOL:
                part = {"functionResponse": {"name": m.name or "tool",
                                             "response": {"result": m.content}}}
                # Responses to parallel function calls go back together in one turn.
                prev = contents[-1] if contents else None
                if prev and prev["role"] == "user" and prev["parts"] and \
                        "functionResponse" in prev["parts"][0]:
                    prev["parts"].append(part)
                else:
                    contents.append({"role": "user", "parts": [part]})
            elif m.role == Role.ASSISTANT:
                parts: list[dict[str, Any]] = []
                if m.content:
                    parts.append({"text": m.content})
                sigs = {b.get("call"): b.get("thoughtSignature") for b in m.thinking_blocks
                        if "thoughtSignature" in b}
                for i, tc in enumerate(m.tool_calls):
                    fc_part: dict[str, Any] = {"functionCall": {"name": tc.name,
                                                                "args": tc.arguments}}
                    if sigs.get(i):  # thinking models require their signature echoed back
                        fc_part["thoughtSignature"] = sigs[i]
                    parts.append(fc_part)
                contents.append({"role": "model", "parts": parts or [{"text": ""}]})
            else:  # USER
                parts = [{"text": m.content}] if m.content else []
                for img in m.images:
                    media, b64 = image_data_uri(img)
                    parts.append({"inlineData": {"mimeType": media, "data": b64}})
                contents.append({"role": "user", "parts": parts or [{"text": ""}]})
        system = {"parts": [{"text": "\n\n".join(system_parts)}]} if system_parts else None
        return system, contents

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        temperature: float = 0.7,
        **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]:
        gen = extract_gen_params(kwargs)
        system, contents = self._convert(messages)
        gen_cfg: dict[str, Any] = {"temperature": temperature}
        if gen["max_tokens"]:
            gen_cfg["maxOutputTokens"] = gen["max_tokens"]
        if gen["thinking"] or gen["effort"] in ("medium", "high"):
            budget = EFFORT_BUDGET.get(gen["effort"] or "medium", 4096)
            gen_cfg["thinkingConfig"] = {"thinkingBudget": budget}
        payload: dict[str, Any] = {"contents": contents, "generationConfig": gen_cfg}
        if system:
            payload["systemInstruction"] = system
        if tools:
            payload["tools"] = [{"functionDeclarations": [
                {"name": t.name, "description": t.description, "parameters": t.parameters}
                for t in tools]}]

        content_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        usage: dict[str, int] | None = None
        signatures: list[dict[str, Any]] = []
        url = f"/models/{self.model}:streamGenerateContent?alt=sse"

        async with self._client.stream("POST", url, json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line or not line.startswith("data:"):
                    continue
                chunk = json.loads(line[len("data:"):].strip())
                if err := chunk.get("error"):
                    msg = err.get("message", "") if isinstance(err, dict) else str(err)
                    raise RuntimeError(f"gemini stream error: {msg}")
                if meta := chunk.get("usageMetadata"):
                    usage = {"input_tokens": int(meta.get("promptTokenCount", 0)),
                             "output_tokens": int(meta.get("candidatesTokenCount", 0))}
                for cand in chunk.get("candidates", []) or []:
                    for part in (cand.get("content") or {}).get("parts", []) or []:
                        if part.get("thought"):
                            continue  # model's internal reasoning: not part of the answer
                        if "text" in part:
                            content_parts.append(part["text"])
                            yield StreamEvent(kind=StreamKind.TOKEN, text=part["text"])
                        elif "functionCall" in part:
                            fc = part["functionCall"]
                            if part.get("thoughtSignature"):
                                signatures.append({"call": len(tool_calls),
                                                   "thoughtSignature": part["thoughtSignature"]})
                            tool_calls.append(ToolCall(
                                id=f"call_{uuid.uuid4().hex[:8]}",
                                name=fc.get("name", ""), arguments=fc.get("args", {}) or {}))

        final = Message(role=Role.ASSISTANT, content="".join(content_parts),
                        tool_calls=tool_calls, usage=usage, thinking_blocks=signatures)
        yield StreamEvent(kind=StreamKind.DONE, message=final)

    async def list_models(self) -> list[str]:
        resp = await self._client.get("/models", params={"pageSize": 1000})
        resp.raise_for_status()
        out = []
        for m in resp.json().get("models", []):
            name = str(m.get("name", "")).removeprefix("models/")
            if name and "generateContent" in (m.get("supportedGenerationMethods") or []):
                out.append(name)
        return out

    async def aclose(self) -> None:
        await self._client.aclose()
