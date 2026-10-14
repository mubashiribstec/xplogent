"""Phone-agent readiness: what is still missing before Telnyx calls can work.

Pure functions over the ``voip`` config + environment, so the Calls view can show a
checklist (and tests can cover every case) without touching the network.
"""

from __future__ import annotations

import ipaddress
import os
from typing import Any
from urllib.parse import urlparse

LOCAL_TTS_HINTS = ("kokoro", "localhost", "127.0.0.1")
_PROVIDER_KEYS = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
                  "gemini": "GEMINI_API_KEY", "openrouter": "OPENROUTER_API_KEY"}


def public_url_problem(url: str) -> str:
    """Why Telnyx can't reach ``url`` ("" when it looks reachable)."""
    if not url:
        return "not set: enter the public https:// address of this server"
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return ("Telnyx needs https://: set up HTTPS with a domain "
                "(xplogent-docker https your.domain.com)")
    host = parsed.hostname or ""
    if host in ("localhost",) or host.endswith(".local"):
        return "this is a local name; Telnyx needs a public domain"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return ""  # a domain name: fine
    if ip.is_private or ip.is_loopback or ip.is_link_local:
        return "this is a private IP; Telnyx needs a public domain with HTTPS"
    return "use a domain name: HTTPS certificates are issued for domains, not IPs"


def _key(env: dict[str, str], name: str) -> bool:
    return bool(env.get(name, "").strip())


def speech_status(v: dict[str, Any], env: dict[str, str]) -> tuple[bool, str]:
    stt = v.get("stt") or {}
    tts = v.get("tts") or {}
    problems = []
    if str(stt.get("provider", "whisper-local")) != "whisper-local":
        if not stt.get("base_url") and not _key(env, str(stt.get("api_key_env", "OPENAI_API_KEY"))):
            problems.append("speech-to-text needs a URL or an OpenAI key")
    base = str(tts.get("base_url") or "https://api.openai.com/v1")
    if "api.openai.com" in base and not _key(env, str(tts.get("api_key_env", "OPENAI_API_KEY"))):
        problems.append("text-to-speech uses OpenAI but no OpenAI key is set: "
                        "choose 'Local on this server' in Speech")
    return (not problems), "; ".join(problems) or (
        "local" if any(h in base for h in LOCAL_TTS_HINTS) else base)


def checklist(v: dict[str, Any], model: str, env: dict[str, str] | None = None) -> list[dict]:
    """Ordered readiness items: ``{id, label, ok, detail, required}``."""
    env = dict(os.environ) if env is None else env
    items: list[dict] = []

    def add(id_: str, label: str, ok: bool, detail: str = "", required: bool = True) -> None:
        items.append({"id": id_, "label": label, "ok": bool(ok), "detail": detail,
                      "required": required})

    url_problem = public_url_problem(str(v.get("public_url", "")))
    add("public_url", "Public HTTPS address", not url_problem, url_problem or str(v.get("public_url")))
    add("webhook_secret", "Webhook secret", bool(v.get("webhook_secret")),
        "" if v.get("webhook_secret") else "click 'Generate webhook secret'")
    key_env = str(v.get("api_key_env", "TELNYX_API_KEY"))
    add("telnyx_key", "Telnyx API key", _key(env, key_env),
        "" if _key(env, key_env) else "paste your Telnyx API v2 key")
    add("connection_id", "Telnyx Voice API application ID", bool(v.get("connection_id")),
        "" if v.get("connection_id") else "needed for outbound calls and the Telnyx test",
        required=False)
    call_model = str(v.get("model") or model)
    provider = call_model.split(":", 1)[0]
    needs = _PROVIDER_KEYS.get(provider)
    model_ok = not needs or _key(env, needs)
    add("model", "Model for calls", model_ok,
        call_model if model_ok else f"{call_model} needs {needs}: add it in Settings or pick an Ollama model")
    ok, detail = speech_status(v, env)
    add("speech", "Speech (listen + speak)", ok, detail)
    add("enabled", "Phone agent switched on", bool(v.get("enabled")),
        "" if v.get("enabled") else "switch 'Phone agent' to on")
    return items
