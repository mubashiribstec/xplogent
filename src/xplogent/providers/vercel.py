"""Vercel AI Gateway — one OpenAI-compatible endpoint for many providers' models.

Spec: ``vercel:<creator>/<model>``, e.g. ``vercel:anthropic/claude-opus-5``. The key is
``AI_GATEWAY_API_KEY`` (Bearer auth); ``GET /models`` lists what the key can use.
"""

from __future__ import annotations

from xplogent.providers.openai import OpenAIProvider

# Cheap, fast models tried first when "Test key" has no model to go on.
_QUICK = ("flash-lite", "flash", "mini", "haiku", "nano", "small")


class VercelGatewayProvider(OpenAIProvider):
    name = "vercel"
    default_base_url = "https://ai-gateway.vercel.sh/v1"
    api_key_env = "AI_GATEWAY_API_KEY"


    async def model_entries(self) -> list[dict]:
        """``GET /models`` with the details the gateway adds (type, tags, pricing)."""
        resp = await self._client.get("/models")
        resp.raise_for_status()
        return [m for m in resp.json().get("data", []) if m.get("id")]


def _price(m: dict) -> float:
    p = m.get("pricing") or {}
    try:
        return float(p.get("input") or 0) + float(p.get("output") or 0)
    except (TypeError, ValueError):
        return 0.0


def pick_quick_model(models: list) -> str:
    """A quick, inexpensive chat model for a key test. With the gateway's details:
    the cheapest paid language model that supports tools (free ones are often
    rate-limited); with bare ids: the first "flash-lite"/"mini"/… name."""
    if models and isinstance(models[0], dict):
        usable = [m for m in models if m.get("type", "language") == "language"
                  and "tool-use" in (m.get("tags") or []) and _price(m) > 0]
        if usable:
            return min(usable, key=_price)["id"]
        models = [m["id"] for m in models]
    ids = list(models)
    chat = [i for i in ids if not any(w in i for w in ("embed", "image", "tts", "whisper", "video", "rerank"))]
    for word in _QUICK:
        for i in chat:
            if word in i.rsplit("/", 1)[-1]:
                return i
    return chat[0] if chat else ""
