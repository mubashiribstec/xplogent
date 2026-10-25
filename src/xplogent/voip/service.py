"""VoipService — wires carrier webhooks and media sockets to CallSession + persistence.

``build_session`` is the testable core: give it config, a store, and (optionally) fake
STT/TTS/LLM and it returns a ready :class:`CallSession` and the new call row id. The
service class adds the real-world plumbing (answer the call, open the stream, run the
socket loop, wrap up the call record, honour do-not-call).
"""

from __future__ import annotations

import re
from typing import Any

from xplogent.core.config import Config, data_dir, load_config
from xplogent.core.logging import get_logger
from xplogent.memory.store import Store
from xplogent.voip.audio import VAD
from xplogent.voip.brain import CallBrain
from xplogent.voip.control import (
    LiveCall,
    calling_hours_problem,
    clean_number,
    resolve_transfer_number,
)
from xplogent.voip.crm import build_payload, crm_enabled, enqueue_delivery
from xplogent.voip.engine import CallSession
from xplogent.voip.fields import build_collector
from xplogent.voip.providers import build_streaming_stt, build_stt, build_tts
from xplogent.voip.templates import get_template

_log = get_logger("voip.service")


AI_DISCLOSURE = ("You're speaking with an AI assistant, and this call is "
                  "transcribed so we can help you better.")
AI_DISCLOSURE_RECORDED = ("You're speaking with an AI assistant, and this call is "
                          "recorded so we can help you better.")
RECORDING_NOTICE = "This call is recorded."


def recording_enabled(v: dict[str, Any]) -> bool:
    return bool((v.get("recording") or {}).get("enabled", True))


def effective_greeting(v: dict[str, Any]) -> str:
    """The greeting the caller actually hears.

    The AI disclosure is injected by the system, not left to editable greeting
    text: if ``disclose_ai`` is on (the default) and the configured greeting
    doesn't already say it, the disclosure sentence goes in front. An operator
    editing the greeting can therefore never silently remove the disclosure
    (vs-Retell brief C3).

    The same goes for recording: while ``voip.recording`` is on, the caller is
    told the call is *recorded* — a greeting that says "transcribed" is switched
    to "recorded", and one that says neither gets the notice in front.
    """
    import re

    greeting = str(v.get("greeting", "")).replace(
        "{company}", str(v.get("company", "our company")))
    recorded = recording_enabled(v)
    if recorded and "record" not in greeting.lower():
        greeting = re.sub(r"\btranscribed\b", "recorded", greeting)
    if v.get("disclose_ai", True) and "ai assistant" not in greeting.lower():
        disclosure = AI_DISCLOSURE_RECORDED if recorded else AI_DISCLOSURE
        greeting = f"{disclosure} {greeting}".strip()
    elif recorded and "record" not in greeting.lower():
        greeting = f"{RECORDING_NOTICE} {greeting}".strip()
    return greeting


def render_template(text: str, variables: dict[str, Any]) -> str:
    """Fill ``{{name}}`` / ``{name}`` placeholders from ``variables`` (C9 dynamic
    variables). Unknown placeholders are left untouched so a typo is visible in
    testing rather than silently blanked."""
    out = text or ""
    for key, value in variables.items():
        if value is None:
            continue
        out = out.replace("{{" + key + "}}", str(value)).replace("{" + key + "}", str(value))
    return out


def call_variables(v: dict[str, Any], info: dict[str, Any],
                   contact: dict[str, Any] | None, customer: str) -> dict[str, Any]:
    """The variables available to greeting/persona/playbook templates on a call."""
    variables: dict[str, Any] = {
        "company": v.get("company", "our company"),
        "caller_name": (contact or {}).get("name", ""),
        "caller_phone": customer,
        "direction": "outbound" if is_outbound(info) else "inbound",
        "line": str(info.get("line", "")),
        "original_called_number": str(info.get("original_called_number") or ""),
        "queue_wait_s": info.get("queue_wait_s") or "",
    }
    for key, value in ((contact or {}).get("fields") or {}).items():
        variables.setdefault(str(key), value)
    return variables


def merge_crm_contact(contact: dict[str, Any] | None, crm: dict[str, Any], phone: str) -> dict[str, Any]:
    """The CRM's view of the caller on top of the local record (which stays the
    offline fallback and is not changed)."""
    out = dict(contact or {"name": "", "phone": phone, "fields": {}})
    fields = dict(out.get("fields") or {})
    if crm.get("name") and not out.get("name"):
        out["name"] = crm["name"]
    if crm.get("company"):
        fields.setdefault("company", crm["company"])
    if crm.get("last_outcome"):
        fields["last_crm_outcome"] = crm["last_outcome"]
    out["fields"] = fields
    if crm.get("notes"):
        out["crm_notes"] = crm["notes"]
    if crm.get("do_not_call"):
        out["do_not_call"] = True
    out.setdefault("source", "crm")
    return out


def load_playbook() -> str:
    """The distilled playbook (written by the training pipeline), if any."""
    path = data_dir() / "voip" / "playbook.md"
    try:
        return path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError:
        return ""


def is_outbound(info: dict[str, str]) -> bool:
    return str(info.get("direction", "")).lower() in ("outgoing", "outbound")


def customer_number(info: dict[str, str]) -> str:
    """The customer's number: who called us (inbound) or who we called (outbound).

    Using ``from`` for both put *our own* number on outbound calls, so the contact
    lookup, DNC request and status updates all hit the wrong record.
    """
    return info.get("to", "") if is_outbound(info) else info.get("from", "")


def _recorder(v: dict[str, Any], call_id: int):
    """A recorder for this call, unless ``voip.recording.enabled`` is false."""
    rec = v.get("recording") or {}
    if not rec.get("enabled", True):
        return None
    from xplogent.voip.recording import CallRecorder, recordings_dir

    return CallRecorder(recordings_dir() / f"{call_id}.wav",
                        max_seconds=float(v.get("max_call_seconds", 900)) + 60)


def _speech_stt(v: dict[str, Any]):
    """The call's speech-to-text: retried once, then the fallback (``voip.stt_fallback``,
    or the built-in Whisper when the main one is a remote service)."""
    from xplogent.voip.providers import ResilientSTT, default_stt_fallback

    main_cfg = v.get("stt", {}) or {}
    fb_cfg = v.get("stt_fallback") or default_stt_fallback(main_cfg)
    return ResilientSTT(build_stt(main_cfg), build_stt(fb_cfg) if fb_cfg else None)


def _speech_tts(v: dict[str, Any]):
    """The call's text-to-speech: retried once, then ``voip.tts_fallback`` if set."""
    from xplogent.voip.providers import ResilientTTS

    fb_cfg = v.get("tts_fallback") or None
    return ResilientTTS(build_tts(v.get("tts", {}) or {}), build_tts(fb_cfg) if fb_cfg else None)


def build_session(cfg: Config, store: Store, info: dict[str, Any], *, send_audio, clear_audio=None,
                  hangup=None, transfer=None, stt=None, tts=None, provider=None,
                  retrieve=None, send_dtmf=None, **session_kw: Any) -> tuple[CallSession, int, CallBrain]:
    """Create a CallSession + its call row. Injected parts make this unit-testable.

    ``info`` may carry ``contact_id`` (a manual call to a chosen contact), ``test``
    (a test call: not counted, never changes the contact) and ``transport``.
    """
    v = cfg.voip or {}
    customer = customer_number(info)
    contact = store.get_contact(int(info["contact_id"])) if info.get("contact_id") else None
    if contact is None and customer:
        contact = store.find_contact_by_phone(customer)
    lookup = info.get("crm_lookup") or {}
    if lookup.get("found") and lookup.get("contact"):
        contact = merge_crm_contact(contact, lookup["contact"], customer)
    if contact is not None and store.is_dnc(contact.get("phone") or customer):
        contact = {**contact, "do_not_call": True}
    direction = str(info.get("direction_label") or ("outbound" if is_outbound(info) else "inbound"))
    call_id = store.start_call(direction, info.get("from", ""), info.get("to", ""),
                               provider_call_id=info.get("call_control_id", ""),
                               contact_id=contact.get("id") if contact else None,
                               test=bool(info.get("test")), transport=str(info.get("transport", "")),
                               line=str(info.get("line", "")),
                               original_called_number=str(info.get("original_called_number") or ""),
                               via_queue=bool(info.get("via_queue")),
                               diversion_reason=str(info.get("diversion_reason") or ""),
                               queue_wait_s=info.get("queue_wait_s"))

    if provider is None:
        from xplogent.providers.registry import build_provider
        provider = build_provider(v.get("model") or cfg.model)
    if retrieve is None:
        retrieve = _make_retriever(cfg, store, int(v.get("knowledge_top_k", 3)))

    persona = f"You work for {v.get('company', 'our company')}."
    if str(info.get("line", "")) == "extension":
        # The extension line is the office PBX: a colleague's extension, an
        # internal test call, or a caller the FreePBX queue overflowed to the AI.
        persona += (" This call is on the internal PBX extension line, not a direct "
                    "external line: the caller is a colleague, an internal test, or "
                    "someone transferred or overflowed from the office phone system.")
    if info.get("via_queue"):
        waited = info.get("queue_wait_s")
        persona += (" The caller waited in the office phone queue"
                    + (f" for about {int(waited)} seconds" if waited else "")
                    + " before reaching you (nobody was free): briefly thank them for "
                      "waiting, once, then help them.")
    if info.get("original_called_number"):
        persona += f" They dialled {info['original_called_number']}."
    silence_cfg = v.get("silence") or {}
    session_kw.setdefault("idle_prompt_s", float(silence_cfg.get("prompt_after_s", 12.0)))
    session_kw.setdefault("idle_hangup_s", float(silence_cfg.get("hangup_after_s", 30.0)))
    session_kw.setdefault("idle_max_prompts", int(silence_cfg.get("max_prompts", 2)))
    dtmf_cfg = v.get("dtmf") or {}
    dtmf_on = bool(dtmf_cfg.get("enabled", True))
    if dtmf_on:
        session_kw.setdefault("dtmf_keys", dtmf_cfg.get("keys") or {})
    can_transfer = transfer is not None and bool(
        resolve_transfer_number(v, transport=str(info.get("transport", ""))))
    variables = call_variables(v, info, contact, customer)
    template = get_template(v.get("template"))
    if template and template["direction"] != ("outbound" if is_outbound(info) else "inbound"):
        # An outbound screener's script must never greet an inbound caller
        # ("is that {{candidate_name}}?"), nor the other way round.
        template = None
    playbook_text = template["playbook"] if template else load_playbook()
    field_configs = list(template["fields"]) if template else []
    field_configs += list(v.get("fields") or [])
    brain = CallBrain(provider, persona=render_template(persona, variables),
                      playbook=render_template(playbook_text, variables),
                      fields=build_collector(field_configs),
                      contact=contact, retrieve=retrieve, store=store, can_transfer=can_transfer,
                      can_send_dtmf=send_dtmf is not None and dtmf_on)
    greeting_v = {**v, "greeting": template["greeting"]} if template else v
    if info.get("via_queue") and str(v.get("queue_greeting") or "").strip():
        greeting_v = {**greeting_v, "greeting": v["queue_greeting"]}   # disclosure rules still apply
    greeting = render_template(effective_greeting(greeting_v), variables)
    vad_cfg = v.get("vad", {}) or {}
    # Live speech-to-text only with the configured provider (an injected stt is a test's).
    stream_stt = session_kw.pop("stream_stt") if "stream_stt" in session_kw else (
        build_streaming_stt(v.get("stt") or {}) if stt is None else None)
    session = CallSession(
        brain=brain, stt=stt or _speech_stt(v), tts=tts or _speech_tts(v),
        send_audio=send_audio, clear_audio=clear_audio, hangup=hangup, transfer=transfer,
        on_turn=lambda speaker, text: store.add_call_turn(call_id, speaker, text),
        greeting=greeting,
        vad=VAD(threshold=float(vad_cfg.get("threshold", 500)),
                silence_ms=int(vad_cfg.get("silence_ms", 700)),
                min_speech_ms=int(vad_cfg.get("min_speech_ms", 200)),
                max_utterance_ms=int(vad_cfg.get("max_utterance_ms", 15000))),
        max_call_s=float(v.get("max_call_seconds", 900)),
        send_dtmf=send_dtmf if dtmf_on else None, recorder=_recorder(v, call_id),
        stream_stt=stream_stt, **session_kw)
    session.dtmf_enabled = dtmf_on
    if lookup:
        session.event("crm_lookup", ("found " + (lookup["contact"].get("name") or "a record")
                                     if lookup.get("found") else "no record" if lookup.get("ok")
                                     else f"failed: {lookup.get('error')}")
                      + f" ({lookup.get('ms', 0)} ms{', cached' if lookup.get('cached') else ''})")
    return session, call_id, brain


def knowledge_gap_path():
    """Where unanswered caller questions are logged (C10): one JSON object per
    line, ``{"ts": ..., "query": ...}`` — the raw material for the knowledge-gap
    report (which questions callers ask that the knowledge base can't answer)."""
    return data_dir() / "voip" / "knowledge_gaps.jsonl"


def top_knowledge_gaps(limit: int = 20) -> list[dict[str, Any]]:
    """The knowledge-gap report: most-asked questions the knowledge base
    could not answer, most frequent first (with when each was last asked)."""
    from collections import Counter

    from xplogent.voip import jsonl

    counts: Counter[str] = Counter()
    latest: dict[str, tuple[str, float]] = {}   # key -> (wording last used, when)
    for rec in jsonl.read(knowledge_gap_path()):
        query = " ".join(str(rec.get("query", "")).split())
        if query:
            counts[query.lower()] += 1
            latest[query.lower()] = (query, float(rec.get("ts") or 0))
    return [{"query": latest[k][0], "count": n, "last_ts": latest[k][1]}
            for k, n in counts.most_common(limit)]


def log_knowledge_gap(query: str) -> None:
    """Best-effort: a logging failure must never break a live call."""
    import time

    from xplogent.voip import jsonl

    try:
        jsonl.append(knowledge_gap_path(), {"ts": time.time(), "query": query[:500]})
    except Exception:  # noqa: BLE001
        _log.exception("could not log knowledge gap")


class _Retriever:
    """Knowledge lookup for one call; owns an embedding client that must be closed."""

    def __init__(self, cfg: Config, store: Store, k: int, *, embedder=None) -> None:
        from xplogent.memory.vector import Embedder
        from xplogent.providers.registry import build_provider

        self.store, self.k = store, k
        self.embedder = embedder or Embedder(build_provider(cfg.embedding_model))

    async def __call__(self, query: str) -> list[str]:
        from xplogent.core.rag import hybrid_search

        hits = await hybrid_search(self.store, self.embedder, query, k=self.k)
        if not hits:
            log_knowledge_gap(query)
        return [h["content"] for h in hits]

    async def aclose(self) -> None:
        await self.embedder.provider.aclose()


def _make_retriever(cfg: Config, store: Store, k: int):
    return _Retriever(cfg, store, k)


def finalize_call(cfg: Config, store: Store, call_id: int, session: CallSession,
                  brain: CallBrain, contact_phone: str = "") -> dict[str, Any]:
    """Write the end-of-call record: outcome, summary, cost; honour do-not-call."""
    st = brain.state
    minutes = session.duration_s / 60.0
    cost = round(minutes * float((cfg.voip or {}).get("cost_per_minute", 0.0)), 4)
    outcome = st.outcome or ("callback" if st.callback else
                             "transfer_failed" if st.transfer_failed else "abandoned")
    summary = st.summary or (f"Callback requested: {st.callback}" if st.callback else "")
    fields_report = brain.fields.report() if getattr(brain, "fields", None) is not None else None
    if fields_report is not None:
        bits = [f"{k}={v}" for k, v in fields_report["values"].items()]
        if fields_report["missing_required"]:
            bits.append("missing: " + ", ".join(fields_report["missing_required"]))
        if st.exit_path:
            bits.append(f"exit: {st.exit_path}")
        if bits:
            summary = (summary + " | Fields: " + "; ".join(bits)).strip(" |")
    store.end_call(call_id, outcome=outcome, summary=summary, cost=cost)
    try:                                # the debug view's record of the call (voip/debug.py)
        from xplogent.voip import debug

        debug.save(store, call_id, session)
        store.prune_call_debug(float(((cfg.voip or {}).get("recording") or {}).get("retention_days", 90)))
    except Exception:  # noqa: BLE001 - diagnostics must never break the call record
        _log.exception("could not save the call's debug trace")
    if getattr(session, "recorder", None) is not None:
        from xplogent.voip.recording import prune

        path = session.recorder.close()
        if path:
            store.set_call_recording(call_id, str(path))
        prune(float(((cfg.voip or {}).get("recording") or {}).get("retention_days", 90)))
    call = store.get_call(call_id) or {}

    def _deliver(result: dict[str, Any]) -> dict[str, Any]:
        # Post-call CRM push (Part 3a): fire-and-forget, retried in crm.deliver;
        # a CRM outage never blocks or loses the call record.
        if crm_enabled(cfg):
            enqueue_delivery(cfg, build_payload(cfg, store, call_id, result, brain, session))
        return result
    if call.get("test"):        # a test call never changes customer records
        return _deliver({"call_id": call_id, "outcome": outcome, "cost": cost, "test": True,
                         "fields": fields_report, "latency_ms": session.latencies_ms})
    if st.do_not_call and contact_phone:
        store.add_dnc(contact_phone, "asked on call")
    if call.get("contact_id"):
        status = {"do_not_call": "do_not_call", "interested": "interested",
                  "not_interested": "not_interested", "callback": "callback"}.get(outcome)
        if status or st.callback:
            store.update_contact_status(call["contact_id"], status or "callback", st.callback)
    return _deliver({"call_id": call_id, "outcome": outcome, "cost": cost,
                     "fields": fields_report, "latency_ms": session.latencies_ms})


class VoipService:
    """Transport-agnostic call service: live-call registry and outbound policy.

    Telnyx specifics live in :class:`~xplogent.voip.telephony.telnyx_trunk.TelnyxTrunk`,
    SIP in :class:`~xplogent.voip.telephony.sip.SipTrunk` (the running lines set
    ``sip_dialer`` / ``sip_extension_dialer``).
    """

    def __init__(self) -> None:
        from xplogent.voip.telephony.telnyx_trunk import TelnyxTrunk

        self.active: dict[int, LiveCall] = {}             # call row id -> live call controls
        self.telnyx = TelnyxTrunk(self)
        # Set by the SIP lines when they are running: async (number, meta) -> result.
        self.sip_dialer: Any = None                       # trunk line (via="sip")
        self.sip_extension_dialer: Any = None             # extension line (via="extension")

    # -- outbound ---------------------------------------------------------------------
    async def dial(self, to_number: str, cfg: Config | None = None, *, contact_id: int | None = None,
                   test: bool = False, via: str = "telnyx",
                   ignore_hours: bool = False) -> dict[str, Any]:
        """Place an outbound call. The do-not-call list is checked *before* dialing:
        ringing a number that opted out is already the violation. Calling hours are
        checked too (in the contact's time zone) unless overridden or a test call."""
        cfg = cfg or load_config()
        v = cfg.voip or {}
        if not v.get("enabled") and not test:
            return {"ok": False, "error": "the phone agent is switched off (Calls → Setup); "
                                          "test calls still work"}
        number = clean_number(to_number)
        if not number and via in ("sip", "extension"):   # PBX extensions are short: 100, *43, 2001
            short = re.sub(r"[\s()-]", "", to_number or "")
            number = short if re.fullmatch(r"[0-9*#+]{2,15}", short) else ""
        if not number:
            return {"ok": False, "error": "not a valid phone number (use +countrycode…)"}
        store = Store(cfg.db_path)
        try:
            if store.is_dnc(number):
                return {"ok": False, "error": "number is on the do-not-call list"}
            contact = store.get_contact(contact_id) if contact_id else store.find_contact_by_phone(number)
        finally:
            store.close()
        if not test and not ignore_hours:
            problem = calling_hours_problem(v, contact)
            if problem:
                return {"ok": False, "error": problem, "hours": True}
        # Both SIP lines share the "sip" transport; the line tag keeps them apart.
        meta = {"contact_id": contact["id"] if contact else None, "test": test,
                "transport": "sip" if via in ("sip", "extension") else via}
        if not test:
            from xplogent.voip import crm

            if crm.lookup_enabled(cfg):
                found = await crm.lookup(cfg, number, direction="outbound",
                                         line="extension" if via == "extension" else via)
                if found.get("found") and (found.get("contact") or {}).get("do_not_call"):
                    return {"ok": False, "error": "the CRM marks this number do-not-call"}
                meta["crm_lookup"] = found
        if via in ("sip", "extension"):
            meta["line"] = via if via == "extension" else "trunk"
        if via in ("sip", "extension"):
            dialer = self.sip_dialer if via == "sip" else self.sip_extension_dialer
            if dialer is None:
                return {"ok": False, "error": "the SIP trunk is not set up (Calls → Telephony)"
                        if via == "sip" else
                        "the PBX extension line is not set up or not switched on (Calls → Telephony)"}
            return await dialer(number, meta)
        return await self.telnyx.dial(number, meta, v)

    # -- live calls -----------------------------------------------------------------------
    def live_by_ref(self, ref: str) -> LiveCall | None:
        if not ref:
            return None
        return next((c for c in self.active.values() if c.ref == ref), None)

    def register(self, live: LiveCall) -> None:
        self.active[live.call_id] = live

    def unregister(self, call_id: int) -> None:
        self.active.pop(call_id, None)

    # -- Telnyx entry points (routes) -----------------------------------------------------
    async def handle_webhook(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.telnyx.handle_webhook(payload)

    async def run_media_socket(self, websocket, token: str) -> None:
        await self.telnyx.run_media_socket(websocket, token)


_SERVICE: VoipService | None = None


def get_service() -> VoipService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = VoipService()
    return _SERVICE
