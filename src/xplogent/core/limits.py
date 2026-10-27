"""Model context-window sizes, for the GUI "context used" gauge.

A small built-in lookup (matched by substring so version suffixes still resolve),
overridable from config via ``model_limits: {"<model>": <tokens>}``. Unknown models
fall back to a conservative default.
"""

from __future__ import annotations

_DEFAULT = 8192

# Substring → context window (tokens). First match wins; order longest/specific first.
_KNOWN: list[tuple[str, int]] = [
    ("claude-sonnet-4", 200000),
    ("claude-opus-4", 200000),
    ("claude-haiku-4", 200000),
    ("claude-3", 200000),
    ("claude", 200000),
    ("gpt-5", 400000),
    ("gpt-4o", 128000),
    ("gpt-4.1", 1000000),
    ("gpt-4-turbo", 128000),
    ("gpt-4", 8192),
    ("o1", 200000),
    ("o3", 200000),
    ("o4", 200000),
    ("gemini-1.5", 1000000),
    ("gemini-2", 1000000),
    ("gemini-3", 1000000),
    ("gemini-4", 1000000),
    ("gemini", 32000),
    ("llama-3.1", 131072),
    ("llama3.1", 131072),
    ("llama-3", 8192),
    ("llama3", 8192),
    ("mistral", 32000),
    ("qwen", 32000),
    ("deepseek", 64000),
]


# Rough USD per 1M tokens (input, output). Local models are free. Best-effort.
_PRICING: list[tuple[str, tuple[float, float]]] = [
    ("claude-opus", (15.0, 75.0)),
    ("claude-sonnet", (3.0, 15.0)),
    ("claude-haiku", (0.8, 4.0)),
    ("gpt-4o-mini", (0.15, 0.6)),
    ("gpt-4o", (2.5, 10.0)),
    ("gpt-4.1", (2.0, 8.0)),
    ("o3", (2.0, 8.0)),
    ("o1", (15.0, 60.0)),
    ("gemini-1.5-pro", (1.25, 5.0)),
    ("gemini-1.5-flash", (0.075, 0.3)),
    ("gemini", (0.3, 1.2)),
]


def estimate_cost(model_spec: str, input_tokens: int, output_tokens: int) -> float:
    """Rough USD cost for a turn. Local providers (ollama) are 0."""
    provider = model_spec.split(":", 1)[0].lower()
    if provider == "ollama":
        return 0.0
    name = model_spec.split(":", 1)[-1].lower()
    for needle, (pin, pout) in _PRICING:
        if needle in name:
            return round(input_tokens / 1e6 * pin + output_tokens / 1e6 * pout, 6)
    return 0.0


def context_window(model_spec: str, overrides: dict[str, int] | None = None) -> int:
    """Return the context window (tokens) for a ``provider:model`` spec."""
    if not model_spec:
        return _DEFAULT
    name = model_spec.split(":", 1)[-1].lower()
    if overrides:
        # exact spec or bare model name override
        for key in (model_spec, name):
            if key in overrides:
                return int(overrides[key])
    for needle, size in _KNOWN:
        if needle in name:
            return size
    return _DEFAULT
