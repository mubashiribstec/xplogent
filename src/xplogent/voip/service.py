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
from xplogent.voip.engine import CallSession
from xplogent.voip.providers import build_stt, build_tts

_log = get_logger("voip.service")


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
                  retrieve=None, **session_kw: Any) -> tuple[CallSession, int, CallBrain]:
    """Create a CallSession + its call row. Injected parts make this unit-testable.

    ``info`` may carry ``contact_id`` (a manual call to a chosen contact), ``test``
    (a test call: not counted, never changes the contact) and ``transport``.
    """
    v = cfg.voip or {}
    customer = customer_number(info)
    contact = store.get_contact(int(info["contact_id"])) if info.get("contact_id") else None
    if contact is None and customer:
        contact = store.find_contact_by_phone(customer)
    if contact is not None and store.is_dnc(contact.get("phone") or customer):
        contact = {**contact, "do_not_call": True}
    direction = str(info.get("direction_label") or ("outbound" if is_outbound(info) else "inbound"))
    call_id = store.start_call(direction, info.get("from", ""), info.get("to", ""),
                               provider_call_id=info.get("call_control_id", ""),
                               contact_id=contact["id"] if contact else None,
                               test=bool(info.get("test")), transport=str(info.get("transport", "")))

    if provider is None:
        from xplogent.providers.registry import build_provider
        provider = build_provider(v.get("model") or cfg.model)
    if retrieve is None:
        retrieve = _make_retriever(cfg, store, int(v.get("knowledge_top_k", 3)))

    persona = f"You work for {v.get('company', 'our company')}."
    can_transfer = transfer is not None and bool(
        resolve_transfer_number(v, transport=str(info.get("transport", ""))))
    brain = CallBrain(provider, persona=persona, playbook=load_playbook(),
                      contact=contact, retrieve=retrieve, store=store, can_transfer=can_transfer)
    greeting = str(v.get("greeting", "")).replace("{company}", str(v.get("company", "our company")))
    vad_cfg = v.get("vad", {}) or {}
    session = CallSession(
        brain=brain, stt=stt or _speech_stt(v), tts=tts or _speech_tts(v),
        send_audio=send_audio, clear_audio=clear_audio, hangup=hangup, transfer=transfer,
        on_turn=lambda speaker, text: store.add_call_turn(call_id, speaker, text),
        greeting=greeting,
        vad=VAD(threshold=float(vad_cfg.get("threshold", 500)),
                silence_ms=int(vad_cfg.get("silence_ms", 700)),
                min_speech_ms=int(vad_cfg.get("min_speech_ms", 200)),
                max_utterance_ms=int(vad_cfg.get("max_utterance_ms", 15000))),
        max_call_s=float(v.get("max_call_seconds", 900)), **session_kw)
    return session, call_id, brain


class _Retriever:
    """Knowledge lookup for one call; owns an embedding client that must be closed."""

    def __init__(self, cfg: Config, store: Store, k: int) -> None:
        from xplogent.memory.vector import Embedder
        from xplogent.providers.registry import build_provider

        self.store, self.k = store, k
        self.embedder = Embedder(build_provider(cfg.embedding_model))

    async def __call__(self, query: str) -> list[str]:
        from xplogent.core.rag import hybrid_search

        hits = await hybrid_search(self.store, self.embedder, query, k=self.k)
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
    store.end_call(call_id, outcome=outcome, summary=summary, cost=cost)
    call = store.get_call(call_id) or {}
    if call.get("test"):        # a test call never changes customer records
        return {"call_id": call_id, "outcome": outcome, "cost": cost, "test": True,
                "latency_ms": session.latencies_ms}
    if st.do_not_call and contact_phone:
        store.add_dnc(contact_phone, "asked on call")
    if call.get("contact_id"):
        status = {"do_not_call": "do_not_call", "interested": "interested",
                  "not_interested": "not_interested", "callback": "callback"}.get(outcome)
        if status or st.callback:
            store.update_contact_status(call["contact_id"], status or "callback", st.callback)
    return {"call_id": call_id, "outcome": outcome, "cost": cost, "latency_ms": session.latencies_ms}


class VoipService:
    """Transport-agnostic call service: live-call registry and outbound policy.

    Telnyx specifics live in :class:`~xplogent.voip.telephony.telnyx_trunk.TelnyxTrunk`,
    SIP in :class:`~xplogent.voip.telephony.sip.SipTrunk` (which sets ``sip_dialer``).
    """

    def __init__(self) -> None:
        from xplogent.voip.telephony.telnyx_trunk import TelnyxTrunk

        self.active: dict[int, LiveCall] = {}             # call row id -> live call controls
        self.telnyx = TelnyxTrunk(self)
        # Set by the SIP trunk when it is running: async (number, meta) -> result.
        self.sip_dialer: Any = None

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
        if not number and via == "sip":         # PBX extensions are short: 100, *43, 2001
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
        meta = {"contact_id": contact["id"] if contact else None, "test": test, "transport": via}
        if via == "sip":
            dialer = self.sip_dialer
            if dialer is None:
                return {"ok": False, "error": "the SIP trunk is not set up (Calls → Telephony)"}
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
