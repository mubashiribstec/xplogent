"""Phone-agent readiness: what is still missing before calls can work.

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
    if str(stt.get("provider", "whisper-local")) == "whisper-local":
        import importlib.util

        if importlib.util.find_spec("faster_whisper") is None:
            problems.append("built-in speech-to-text needs faster-whisper: pip install 'xplogent[voip]'")
    else:
        if not stt.get("base_url") and not _key(env, str(stt.get("api_key_env", "OPENAI_API_KEY"))):
            problems.append("speech-to-text needs a URL or an OpenAI key")
    base = str(tts.get("base_url") or "https://api.openai.com/v1")
    if "api.openai.com" in base and not _key(env, str(tts.get("api_key_env", "OPENAI_API_KEY"))):
        problems.append("text-to-speech uses OpenAI but no OpenAI key is set: "
                        "choose 'Local on this server' in Speech")
    return (not problems), "; ".join(problems) or (
        "local" if any(h in base for h in LOCAL_TTS_HINTS) else base)


def _capacity(add, id_: str, label: str, st: dict[str, Any]) -> None:
    """Warn before a SIP line fills up (the next caller gets "busy"), and when it did."""
    if not st.get("running") or not st.get("max_calls"):
        return
    calls, cap, busy = int(st.get("calls") or 0), int(st["max_calls"]), int(st.get("busy_24h") or 0)
    if busy:
        add(id_, label, False,
            f"{busy} caller{'s' if busy != 1 else ''} got busy in the last 24 h (line full at "
            f"{cap} calls): raise max_calls or give the extension a busy fallback in FreePBX",
            required=False)
    elif calls >= max(1, cap - 1):
        add(id_, label, False, f"{calls} of {cap} calls in use: the next callers may get busy",
            required=False)
    else:
        add(id_, label, True, f"{calls} of {cap} calls in use", required=False)


def checklist(v: dict[str, Any], model: str, env: dict[str, str] | None = None,
              sip_status: dict[str, Any] | None = None,
              sip_extension_status: dict[str, Any] | None = None) -> list[dict]:
    """Ordered readiness items: ``{id, label, ok, detail, required}``.

    Which telephony items appear depends on ``voip.telephony``: ``telnyx`` (Voice API,
    needs a public HTTPS address), ``sip`` (built-in SIP trunk client) or ``both``.
    """
    env = dict(os.environ) if env is None else env
    items: list[dict] = []
    mode = str(v.get("telephony") or "telnyx")

    def add(id_: str, label: str, ok: bool, detail: str = "", required: bool = True) -> None:
        items.append({"id": id_, "label": label, "ok": bool(ok), "detail": detail,
                      "required": required})

    if mode in ("telnyx", "both"):
        url_problem = public_url_problem(str(v.get("public_url", "")))
        add("public_url", "Public HTTPS address", not url_problem, url_problem or str(v.get("public_url")))
        secret = str(v.get("webhook_secret") or "")
        add("webhook_secret", "Webhook secret", len(secret) >= 16,
            "" if len(secret) >= 16 else ("click 'Generate webhook secret'" if not secret else
                                          "too short to be safe: click 'Regenerate webhook secret'"))
        add("telnyx_signature", "Telnyx webhook signature check", bool(v.get("telnyx_public_key")),
            "on" if v.get("telnyx_public_key") else
            "recommended: paste your Telnyx public key in the Telnyx card", required=False)
        key_env = str(v.get("api_key_env", "TELNYX_API_KEY"))
        add("telnyx_key", "Telnyx API key", _key(env, key_env),
            "" if _key(env, key_env) else "paste your Telnyx API v2 key")
        add("connection_id", "Telnyx Voice API application ID", bool(v.get("connection_id")),
            "" if v.get("connection_id") else "needed for outbound calls and the Telnyx test",
            required=False)
    if mode in ("sip", "both"):
        sip = v.get("sip") or {}
        configured = bool(sip.get("server") and sip.get("username"))
        add("sip_account", "SIP trunk account", configured and _key(env, "SIP_PASSWORD"),
            "" if configured and _key(env, "SIP_PASSWORD") else
            "enter the SIP server, username and password under Telephony")
        st = sip_status or {}
        if sip.get("register", True):
            add("sip_registered", "SIP registration", bool(st.get("registered")),
                "registered" if st.get("registered") else (st.get("error") or "not registered yet"))
        _capacity(add, "sip_capacity", "SIP trunk capacity", st)
    ext = v.get("sip_extension") or {}
    if ext.get("enabled"):
        configured = bool(ext.get("server") and ext.get("username"))
        add("sip_extension_account", "PBX extension account",
            configured and _key(env, "SIP_EXTENSION_PASSWORD"),
            "" if configured and _key(env, "SIP_EXTENSION_PASSWORD") else
            "enter the PBX address, extension number and password in the PBX extension card")
        if ext.get("register", True):
            st = sip_extension_status or {}
            add("sip_extension_registered", "PBX extension registration", bool(st.get("registered")),
                "registered" if st.get("registered") else (st.get("error") or "not registered yet"))
        _capacity(add, "sip_extension_capacity", "PBX extension capacity", sip_extension_status or {})
    call_model = str(v.get("model") or model)
    provider = call_model.split(":", 1)[0]
    needs = _PROVIDER_KEYS.get(provider)
    model_ok = not needs or _key(env, needs)
    add("model", "Model for calls", model_ok,
        call_model if model_ok else f"{call_model} needs {needs}: add it in Settings or pick an Ollama model")
    ok, detail = speech_status(v, env)
    add("speech", "Speech (listen + speak)", ok, detail)
    rec_on = bool((v.get("recording") or {}).get("enabled", True))
    claims = "record" in str(v.get("greeting", "")).lower()
    if claims and not rec_on:
        add("greeting", "Greeting is truthful", False,
            "the greeting says calls are recorded but recording is off: change one of them",
            required=False)
    from xplogent.voip.config_check import check_voip

    bad = check_voip(v)
    add("settings", "Settings are valid", not bad,
        "; ".join(bad[:3]) + (f" (+{len(bad) - 3} more)" if len(bad) > 3 else ""), required=False)
    add("enabled", "Phone agent switched on", bool(v.get("enabled")),
        "" if v.get("enabled") else "switch 'Phone agent' to on")
    return items
