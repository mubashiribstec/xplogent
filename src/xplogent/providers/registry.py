"""Provider registry — build a provider from a ``provider:model`` spec."""

from __future__ import annotations

from typing import Any

from xplogent.providers.anthropic import AnthropicProvider
from xplogent.providers.base import Provider
from xplogent.providers.gemini import GeminiProvider
from xplogent.providers.ollama import OllamaProvider
from xplogent.providers.openai import OpenAIProvider
from xplogent.providers.openrouter import OpenRouterProvider

_PROVIDERS: dict[str, type[Provider]] = {
    "ollama": OllamaProvider,
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
    "openrouter": OpenRouterProvider,
    "gemini": GeminiProvider,
}


def available_providers() -> list[str]:
    return sorted(_PROVIDERS)


def register_provider(name: str, cls: type[Provider]) -> None:
    """Register a custom provider (used by plugins)."""
    _PROVIDERS[name] = cls


def build_provider(spec: str, **kwargs: Any) -> Provider:
    """Create a provider from ``"provider:model"`` (e.g. ``"ollama:llama3.1"``)."""
    provider_name, _, model = spec.partition(":")
    if not model:
        model = provider_name
    if provider_name not in _PROVIDERS:
        raise ValueError(
            f"Unknown provider '{provider_name}'. Available: {', '.join(available_providers())}"
        )
    return _PROVIDERS[provider_name](model=model, **kwargs)
