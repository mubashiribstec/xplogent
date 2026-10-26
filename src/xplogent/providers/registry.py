"""Provider registry — build a provider from a ``provider:model`` spec."""

from __future__ import annotations

from typing import Any

from xplogent.providers.anthropic import AnthropicProvider
from xplogent.providers.base import Provider
from xplogent.providers.gemini import GeminiProvider
from xplogent.providers.ollama import LocalOllamaProvider, OllamaProvider
from xplogent.providers.openai import OpenAIProvider
from xplogent.providers.openrouter import OpenRouterProvider

_PROVIDERS: dict[str, type[Provider]] = {
    "ollama": OllamaProvider,
    "ollama-local": LocalOllamaProvider,
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


def _make(spec: str, **kwargs: Any) -> Provider:
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
    """Spec for the configured backup model (enabled or not): a full spec as given, a
    model on a named Ollama server (``fallback.server``), or the local Docker Ollama."""
    model = str(fb.get("model") or "llama3.2:3b").strip()
    if ":" in model and model.split(":", 1)[0] in _PROVIDERS:
        return model
    server = str(fb.get("server") or "").strip()
    return f"ollama:{model}@{server}" if server else f"ollama-local:{model}"


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
