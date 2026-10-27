"""Turn a provider error into what happened and what to do, in plain words.

Used by the model tests in the dashboard. The cases follow OpenRouter's documented
errors (https://openrouter.ai/docs/api-reference/errors and /limits), which also cover
the other OpenAI-compatible providers: 401 bad key, 402 credits / key limit, 429 rate
limits (OpenRouter's own free-model caps vs. the upstream provider), 404 unknown model,
502/503 provider down, and errors sent mid-stream after a 200.
"""

from __future__ import annotations

import asyncio
from typing import Any

_KEY_PLACE = {"openrouter": "Settings → API keys → OPENROUTER_API_KEY",
              "vercel": "Settings → API keys → AI_GATEWAY_API_KEY",
              "openai": "Settings → API keys → OPENAI_API_KEY",
              "anthropic": "Settings → API keys → ANTHROPIC_API_KEY",
              "gemini": "Settings → API keys → GEMINI_API_KEY"}


def diagnose(exc: BaseException, spec: str = "", quota: dict[str, Any] | None = None) -> dict[str, str]:
    """``{code, reason, fix}`` for a failed request to ``spec`` (``provider:model``)."""
    import httpx

    provider, _, model = spec.partition(":")
    where = _KEY_PLACE.get(provider, "Settings → API keys")
    details: dict[str, Any] = dict(getattr(exc, "details", {}) or {})
    status = details.get("status")
    if status is None and isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
    raw = str(details.get("raw") or details.get("message") or exc)[:300]
    text = f"{raw} {exc}".lower()
    upstream = details.get("provider") or ""
    free = model.endswith(":free")

    def out(code: str, reason: str, fix: str) -> dict[str, str]:
        return {"code": code, "reason": reason, "fix": fix}

    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException)):
        return out("timeout", f"{spec} did not answer in time.",
                   "Try again; if it keeps happening pick a faster model (a free model can be slow when busy).")
    if isinstance(exc, (httpx.ConnectError, httpx.NetworkError)):
        return out("unreachable", f"Could not connect: {exc}"[:200],
                   "Check the server's internet connection (or the Ollama server's address).")
    if isinstance(exc, ValueError) and "ollama server named" in text:
        return out("unknown_server", str(exc), "Add that server under Settings → Ollama servers, or pick another model.")
    if status == 401 or "api key not valid" in text or "api_key_invalid" in text or "invalid api key" in text or "user not found" in text or "no auth credentials" in text:
        return out("bad_key", "The API key was refused (wrong, revoked or disabled).",
                   f"Create a new key at the provider and paste it in {where}.")
    if status == 402 or "insufficient credits" in text or "requires more credits" in text:
        src = details.get("limit_source", "")
        if src == "openrouter_key_limit":
            return out("key_limit", "This key's spending limit is used up.",
                       "Raise the key's limit on openrouter.ai (Keys), or wait for it to reset.")
        return out("credits", "Not enough credits for this model.",
                   "Add credits at the provider, or use a free model (ending in :free).")
    if status == 403:
        return out("forbidden", f"Refused by the provider: {raw}",
                   "The key may not be allowed to use this model (guardrail/moderation), try another model.")
    if provider == "gemini" and (status == 404 or "is not found for api version" in text):
        return out("unknown_model",
                   f"Google doesn't serve {model} to this key. Gemini 2.0 and 1.5 are shut down, and 2.5 "
                   "models are only available to accounts that used them before.",
                   "Use gemini:gemini-3.6-flash (Google's pick for new projects). Settings → Browse provider "
                   "models → gemini lists exactly what your key can use.")
    if status == 404 or "no endpoints found" in text or "not a valid model" in text or "model not found" in text:
        return out("unknown_model", f"The model {model or spec} was not found.",
                   "Check the model id (Settings → Browse provider models lists the exact ids).")
    if status == 429 or "rate limit" in text or "rate-limited" in text:
        if quota and quota.get("free_limit") and quota.get("free_remaining") == 0:
            return out("free_daily_cap",
                       f"All {quota['free_limit']} free-model requests for today are used.",
                       "They reset at 00:00 UTC. Buying $10 of OpenRouter credits raises the cap to 1,000 a day; "
                       "until then the backup model answers calls.")
        if "upstream" in text or upstream:
            return out("rate_limited_upstream",
                       f"{model or spec} is busy at {upstream or 'its provider'} (shared free pool), not a problem "
                       "with your key.",
                       "Retry in a minute or pick another model (e.g. Nemotron 3 Ultra (free)). Keep a backup "
                       "model on so calls are still answered.")
        return out("rate_limited", "Too many requests right now"
                   + (" (free models: 20 per minute)" if free else "") + ".",
                   "Wait a minute and retry; keep a backup model on so calls are still answered.")
    if "high demand" in text or "overloaded" in text:
        return out("busy", f"{model or spec} is overloaded at the provider right now (temporary; your key is fine).",
                   "Retry in a minute, or pick another model (e.g. gemini:gemini-3.6-flash). Keep a backup "
                   "model on so calls are still answered.")
    if status in (502, 503) or ("provider returned error" in text and status is None):
        return out("provider_down", f"The model is down or no provider is available: {raw}",
                   "Retry later or pick another model.")
    if status == 400:
        return out("bad_request", f"The provider rejected the request: {raw}",
                   "This model may not support tools; pick one that does (the free presets do).")
    if details.get("mid_stream"):
        return out("mid_stream", f"The model failed while answering: {raw}", "Retry, or pick another model.")
    return out("error", f"{type(exc).__name__}: {raw}"[:300], "Retry; if it keeps failing, pick another model.")
