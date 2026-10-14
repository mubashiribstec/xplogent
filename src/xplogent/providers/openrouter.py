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
