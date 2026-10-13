"""VoipService — wires carrier webhooks and media sockets to CallSession + persistence.

``build_session`` is the testable core: give it config, a store, and (optionally) fake
STT/TTS/LLM and it returns a ready :class:`CallSession` and the new call row id. The
service class adds the real-world plumbing (answer the call, open the stream, run the
socket loop, wrap up the call record, honour do-not-call).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from typing import Any

from xplogent.core.config import Config, data_dir, load_config
from xplogent.core.logging import get_logger
from xplogent.memory.store import Store
from xplogent.voip.audio import VAD
from xplogent.voip.brain import CallBrain
from xplogent.voip.engine import CallSession
from xplogent.voip.providers import build_stt, build_tts
from xplogent.voip.telephony.telnyx import (
    TelnyxClient,
    clear_message,
    media_message,
    parse_stream_message,
    parse_webhook,
)

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


def build_session(cfg: Config, store: Store, info: dict[str, str], *, send_audio, clear_audio=None,
                  hangup=None, transfer=None, stt=None, tts=None, provider=None,
                  retrieve=None) -> tuple[CallSession, int, CallBrain]:
    """Create a CallSession + its call row. Injected parts make this unit-testable."""
    v = cfg.voip or {}
    customer = customer_number(info)
    contact = store.find_contact_by_phone(customer) if customer else None
    if contact is not None and store.is_dnc(customer):
        contact = {**contact, "do_not_call": True}
    direction = "outbound" if is_outbound(info) else "inbound"
    call_id = store.start_call(direction, info.get("from", ""), info.get("to", ""),
                               provider_call_id=info.get("call_control_id", ""),
                               contact_id=contact["id"] if contact else None)

    if provider is None:
        from xplogent.providers.registry import build_provider
        provider = build_provider(v.get("model") or cfg.model)
    if retrieve is None:
        retrieve = _make_retriever(cfg, store, int(v.get("knowledge_top_k", 3)))

    persona = f"You work for {v.get('company', 'our company')}."
    brain = CallBrain(provider, persona=persona, playbook=load_playbook(),
                      contact=contact, retrieve=retrieve, store=store)
    greeting = str(v.get("greeting", "")).replace("{company}", str(v.get("company", "our company")))
    vad_cfg = v.get("vad", {}) or {}
    session = CallSession(
        brain=brain, stt=stt or build_stt(v.get("stt", {}) or {}),
        tts=tts or build_tts(v.get("tts", {}) or {}),
        send_audio=send_audio, clear_audio=clear_audio, hangup=hangup, transfer=transfer,
        on_turn=lambda speaker, text: store.add_call_turn(call_id, speaker, text),
        greeting=greeting,
        vad=VAD(threshold=float(vad_cfg.get("threshold", 500)),
                silence_ms=int(vad_cfg.get("silence_ms", 700))),
        max_call_s=float(v.get("max_call_seconds", 900)))
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
    outcome = st.outcome or ("callback" if st.callback else "abandoned")
    summary = st.summary or (f"Callback requested: {st.callback}" if st.callback else "")
    store.end_call(call_id, outcome=outcome, summary=summary, cost=cost)
    call = store.get_call(call_id) or {}
    if st.do_not_call and contact_phone:
        store.add_dnc(contact_phone, "asked on call")
    if call.get("contact_id"):
        status = {"do_not_call": "do_not_call", "interested": "interested",
                  "not_interested": "not_interested", "callback": "callback"}.get(outcome)
        if status or st.callback:
            store.update_contact_status(call["contact_id"], status or "callback", st.callback)
    return {"call_id": call_id, "outcome": outcome, "cost": cost, "latency_ms": session.latencies_ms}


class VoipService:
    PENDING_TTL = 120.0      # a stream token unused this long belongs to a call that never connected
    CALL_TTL = 4 * 3600.0    # call info whose hangup webhook was lost

    def __init__(self) -> None:
        self.pending: dict[str, dict[str, str]] = {}      # stream token -> call info
        self.by_call: dict[str, dict[str, str]] = {}      # call_control_id -> info
        self.active: dict[int, dict[str, Any]] = {}       # call row id -> live info
        self._tasks: set[asyncio.Task] = set()
        self._seen_at: dict[str, float] = {}              # token / call id -> created

    def _remember(self, table: dict, key: str, value: dict[str, str]) -> None:
        table[key] = value
        self._seen_at[key] = time.monotonic()

    def _expire(self) -> None:
        now = time.monotonic()
        for table, ttl in ((self.pending, self.PENDING_TTL), (self.by_call, self.CALL_TTL)):
            for key in [k for k in table if now - self._seen_at.get(k, now) > ttl]:
                table.pop(key, None)
                self._seen_at.pop(key, None)

    @staticmethod
    def _blocked(cfg: Config, info: dict[str, str]) -> bool:
        """Outbound calls to a do-not-call number must not go ahead."""
        if not is_outbound(info):
            return False
        store = Store(cfg.db_path)
        try:
            return store.is_dnc(customer_number(info))
        finally:
            store.close()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- outbound ---------------------------------------------------------------------
    async def dial(self, to_number: str, cfg: Config | None = None) -> dict[str, Any]:
        """Place an outbound call. The do-not-call list is checked *before* dialing:
        ringing a number that opted out is already the violation."""
        cfg = cfg or load_config()
        v = cfg.voip or {}
        store = Store(cfg.db_path)
        try:
            if store.is_dnc(to_number):
                return {"ok": False, "error": "number is on the do-not-call list"}
        finally:
            store.close()
        from_number, connection = str(v.get("from_number", "")), str(v.get("connection_id", ""))
        if not (from_number and connection):
            return {"ok": False, "error": "voip.from_number and voip.connection_id are required"}
        client = TelnyxClient(api_key_env=str(v.get("api_key_env", "TELNYX_API_KEY")))
        try:
            res = await client.dial(to_number, from_number, connection)
        finally:
            await client.aclose()
        return {"ok": True, "call": res.get("data", res)}

    # -- carrier webhook -------------------------------------------------------------
    async def handle_webhook(self, payload: dict[str, Any]) -> dict[str, Any]:
        cfg = load_config()
        v = cfg.voip or {}
        if not v.get("enabled"):
            return {"ok": False, "error": "voip is disabled"}
        ev = parse_webhook(payload)
        self._expire()
        ccid = ev["call_control_id"]

        def client() -> TelnyxClient:
            return TelnyxClient(api_key_env=str(v.get("api_key_env", "TELNYX_API_KEY")))

        if ev["event"] == "call.initiated":
            self._remember(self.by_call, ccid, ev)
            if not is_outbound(ev):
                c = client()
                self._spawn(self._act(c, c.answer(ccid)))
        elif ev["event"] == "call.answered":
            info = {**self.by_call.get(ccid, ev), "call_control_id": ccid}
            if self._blocked(cfg, info):
                _log.warning("outbound call to a do-not-call number: hanging up")
                c = client()
                self._spawn(self._act(c, c.hangup(ccid)))
                return {"ok": True, "blocked": "do_not_call"}
            token = uuid.uuid4().hex
            self._remember(self.pending, token, info)
            base = str(v.get("public_url", "")).rstrip("/")
            ws_url = base.replace("https://", "wss://").replace("http://", "ws://") + f"/ws/telephony/{token}"
            c = client()
            self._spawn(self._act(c, c.start_stream(ccid, ws_url)))
        elif ev["event"] == "call.hangup":
            self.by_call.pop(ccid, None)
            self._seen_at.pop(ccid, None)
        return {"ok": True}

    @staticmethod
    async def _act(client: TelnyxClient, coro) -> None:
        try:
            await coro
        except Exception:  # noqa: BLE001
            _log.exception("telnyx action failed")
        finally:
            await client.aclose()

    # -- media socket -------------------------------------------------------------------
    async def run_media_socket(self, websocket, token: str) -> None:
        self._expire()
        info = self.pending.pop(token, None)
        self._seen_at.pop(token, None)
        if info is None:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        cfg = load_config()
        v = cfg.voip or {}
        store = Store(cfg.db_path)
        client = TelnyxClient(api_key_env=str(v.get("api_key_env", "TELNYX_API_KEY")))
        ccid = info.get("call_control_id", "")
        stream_id = {"id": ""}

        async def send_audio(chunk: bytes) -> None:
            await websocket.send_text(media_message(chunk, stream_id["id"]))

        async def clear_audio() -> None:
            await websocket.send_text(clear_message(stream_id["id"]))

        async def hangup() -> None:
            await client.hangup(ccid)

        async def transfer(reason: str) -> None:
            number = str(v.get("transfer_number", ""))
            if number:
                await client.transfer(ccid, number)
            else:
                await client.hangup(ccid)

        session, call_id, brain = build_session(
            cfg, store, info, send_audio=send_audio, clear_audio=clear_audio,
            hangup=hangup, transfer=transfer)
        self.active[call_id] = {"from": customer_number(info), "started": session.started}
        started = False
        try:
            while not session.finished.is_set():
                try:
                    raw = await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
                except TimeoutError:
                    continue
                msg = parse_stream_message(raw)
                if msg["event"] == "start":
                    stream_id["id"] = msg["stream_id"]
                # Speak only once the carrier has told us the stream id (needed to
                # address outbound audio); some carriers skip `start`, so media works too.
                if not started and msg["event"] in ("start", "media"):
                    started = True
                    await session.start()
                if msg["event"] == "media":
                    await session.on_audio(msg["audio"])
                elif msg["event"] == "stop":
                    break
        except Exception:  # noqa: BLE001 - socket closed / carrier hung up
            pass
        finally:
            self.active.pop(call_id, None)
            await session.stop()
            result = finalize_call(cfg, store, call_id, session, brain, customer_number(info))
            _log.info("call %s finished: %s", call_id, result)
            closers = [session.stt.aclose, session.tts.aclose, client.aclose,
                       brain.provider.aclose]
            if hasattr(brain.retrieve, "aclose"):
                closers.append(brain.retrieve.aclose)
            for closer in closers:
                with contextlib.suppress(Exception):
                    await closer()
            store.close()
            with contextlib.suppress(Exception):
                await websocket.close()


_SERVICE: VoipService | None = None


def get_service() -> VoipService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = VoipService()
    return _SERVICE
