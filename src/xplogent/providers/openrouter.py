"""OpenRouter provider — OpenAI-compatible gateway to 200+ hosted models."""

from __future__ import annotations

from xplogent.providers.openai import OpenAIProvider


class OpenRouterProvider(OpenAIProvider):
    name = "openrouter"
    default_base_url = "https://openrouter.ai/api/v1"
    api_key_env = "OPENROUTER_API_KEY"

    def _extra_headers(self, headers: dict[str, str]) -> None:
        # Optional attribution headers recommended by OpenRouter.
        headers.setdefault("HTTP-Referer", "https://github.com/mubashiribstec/xplogent")
        headers.setdefault("X-Title", "Xplogent Agent")

    async def key_info(self) -> dict:
        """This key's limits (GET /key): free-model requests used today, credit limit."""
        resp = await self._client.get("/key")
        resp.raise_for_status()
        return parse_key_info(resp.json())


def parse_key_info(body: dict) -> dict:
    d = body.get("data") or body
    free = d.get("free_model_daily_requests") or {}
    return {"free_used": free.get("used"), "free_limit": free.get("limit"),
            "free_remaining": free.get("remaining"), "is_free_tier": d.get("is_free_tier"),
            "limit": d.get("limit"), "limit_remaining": d.get("limit_remaining"),
            "usage_daily": d.get("usage_daily")}
