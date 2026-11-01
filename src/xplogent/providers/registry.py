"""Provider registry — build a provider from a ``provider:model`` spec."""

from __future__ import annotations

from typing import Any

from xplogent.providers.anthropic import AnthropicProvider
from xplogent.providers.base import Provider
from xplogent.providers.gemini import GeminiProvider
from xplogent.providers.ollama import OllamaProvider
from xplogent.providers.openai import OpenAIProvider
from xplogent.providers.openrouter import OpenRouterProvider
from xplogent.providers.vercel import VercelGatewayProvider

_PROVIDERS: dict[str, type[Provider]] = {
    "ollama": OllamaProvider,
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
    "openrouter": OpenRouterProvider,
    "gemini": GeminiProvider,
    "vercel": VercelGatewayProvider,
}


def available_providers() -> list[str]:
    return sorted(_PROVIDERS)


def register_provider(name: str, cls: type[Provider]) -> None:
    """Register a custom provider (used by plugins)."""
    _PROVIDERS[name] = cls


class NoModelProvider(Provider):
    """Stands in for a model setting left empty (e.g. no embedding model): chatting says
    what to set, and embeddings switch off for good (full-text search still works)."""

    name = "none"

    def __init__(self, model: str = "", **kwargs: Any) -> None:
        super().__init__(model)

    async def stream(self, messages, tools=None, **kwargs):  # type: ignore[override]
        raise RuntimeError("no model is set: choose one in Settings → Models")
        yield  # pragma: no cover - makes this an async generator

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError("no embedding model set: search uses full-text matching")


def _make(spec: str, **kwargs: Any) -> Provider:
    if not (spec or "").strip():
        return NoModelProvider()
    provider_name, sep, model = spec.partition(":")
    if sep and not model:
        raise ValueError(f"no model in spec '{spec}' — write it as 'provider:model'")
    if not model:
        model = provider_name
    if provider_name not in _PROVIDERS:
        raise ValueError(
            f"Unknown provider '{provider_name}'. Available: {', '.join(available_providers())}"
        )
    return _PROVIDERS[provider_name](model=model, **kwargs)


def backup_target_spec(fb: dict) -> str:
    """Spec for the configured backup model (enabled or not): a full spec as given, or a
    bare Ollama model name on your Ollama server (``fallback.server`` names one of them;
    older settings). "" when none is set. Nothing runs locally any more."""
    model = str(fb.get("model") or "").strip()
    if not model:
        return ""
    if model.split(":", 1)[0] in _PROVIDERS and ":" in model:
        return model
    server = str(fb.get("server") or "").strip()
    return f"ollama:{model}@{server}" if server else f"ollama:{model}"


def backup_spec() -> str:
    """The backup model spec when the backup is enabled (config ``fallback``), else ""."""
    try:
        from xplogent.core.config import load_config

        fb = load_config().raw.get("fallback") or {}
    except Exception:  # noqa: BLE001
        return ""
    if not fb.get("enabled") or not str(fb.get("model") or "").strip():
        return ""
    return backup_target_spec(fb)


def build_provider(spec: str, *, fallback: bool = True, **kwargs: Any) -> Provider:
    """Create a provider from ``"provider:model"`` (e.g. ``"ollama:llama3.1"``).

    When the backup model is enabled, the provider is wrapped so a failed request is
    answered by the backup instead (see :mod:`xplogent.providers.fallback`).
    """
    primary = _make(spec, **kwargs)
    backup = backup_spec() if fallback else ""
    if not backup or backup == spec:
        return primary
    from xplogent.providers.fallback import FallbackProvider

    return FallbackProvider(primary, lambda: _make(backup), backup_spec=backup)
