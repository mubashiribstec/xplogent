"""Telnyx Voice API transport: webhook state machine, media stream loop, dialing.

Flow (inbound): ``call.initiated`` → answer; ``call.answered`` → ``streaming_start`` to our
``/ws/telephony/<one-time token>``; the stream then feeds a :class:`CallSession`.
Outbound calls are placed with :meth:`TelnyxTrunk.dial` and join the same flow when
answered. Transfers carry ``client_state="xfer:<call id>"`` so the new leg's
``call.bridged`` / ``call.hangup`` tells the session whether the hand-off worked.

:class:`~xplogent.voip.service.VoipService` owns the transport-agnostic parts (live-call
registry, dial policy); this class owns everything Telnyx-specific.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from typing import TYPE_CHECKING, Any

from xplogent.core.config import Config, load_config
from xplogent.core.logging import get_logger
from xplogent.memory.store import Store
from xplogent.voip.control import LiveCall, resolve_transfer_number
from xplogent.voip.telephony.telnyx import (
    TelnyxClient,
    clear_message,
    media_message,
    parse_stream_message,
    parse_webhook,
)

if TYPE_CHECKING:
    from xplogent.voip.service import VoipService

_log = get_logger("voip.telnyx")


class TelnyxTrunk:
    PENDING_TTL = 120.0      # a stream token unused this long belongs to a call that never connected
    CALL_TTL = 4 * 3600.0    # call info whose hangup webhook was lost

    def __init__(self, service: VoipService) -> None:
        self.service = service
        self.pending: dict[str, dict[str, Any]] = {}      # stream token -> call info
        self.by_call: dict[str, dict[str, Any]] = {}      # call_control_id -> info
        self.outbound: dict[str, dict[str, Any]] = {}     # call_control_id -> {contact_id, test}
        self._tasks: set[asyncio.Task] = set()
        self._seen_at: dict[str, float] = {}              # token / call id -> created
        self.call_events: dict[str, list[dict[str, Any]]] = {}   # call id -> webhook events (debug view)
        self.lookups: dict[str, asyncio.Task] = {}      # call id -> pre-call CRM lookup in flight

    def _log_event(self, ccid: str, ev: dict[str, Any], leg: str = "") -> None:
        if not ccid:
            return
        while ccid not in self.call_events and len(self.call_events) >= 50:
            self.call_events.pop(next(iter(self.call_events)))
        events = self.call_events.setdefault(ccid, [])
        if len(events) < 100:
            events.append({"at": time.time(), "event": ev.get("event", ""), "leg": leg,
                           **({"digit": ev["digit"]} if ev.get("digit") else {})})

    def _remember(self, table: dict, key: str, value: dict[str, Any]) -> None:
        table[key] = value
        self._seen_at[key] = time.monotonic()

    def _expire(self) -> None:
        now = time.monotonic()
        for table, ttl in ((self.pending, self.PENDING_TTL), (self.by_call, self.CALL_TTL),
                           (self.outbound, self.CALL_TTL)):
            for key in [k for k in table if now - self._seen_at.get(k, now) > ttl]:
                table.pop(key, None)
                self._seen_at.pop(key, None)

    @staticmethod
    def _blocked(cfg: Config, info: dict[str, Any]) -> bool:
        """Outbound calls to a do-not-call number must not go ahead."""
        from xplogent.voip.service import customer_number, is_outbound

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

    @staticmethod
    def _client(v: dict[str, Any]) -> TelnyxClient:
        return TelnyxClient(api_key_env=str(v.get("api_key_env", "TELNYX_API_KEY")))

    # -- outbound -------------------------------------------------------------------
    async def dial(self, number: str, meta: dict[str, Any], v: dict[str, Any]) -> dict[str, Any]:
        """Place the call (policy checks already done by the service)."""
        from_number, connection = str(v.get("from_number", "")), str(v.get("connection_id", ""))
        if not (from_number and connection):
            return {"ok": False, "error": "set your Telnyx number and Voice API application ID first"}
        client = self._client(v)
        try:
            res = await client.dial(number, from_number, connection)
        except Exception as exc:  # noqa: BLE001 - shown in the dashboard
            return {"ok": False, "error": f"Telnyx refused the call: {exc}"[:300]}
        finally:
            await client.aclose()
        data = res.get("data", res) if isinstance(res, dict) else {}
        ccid = str(data.get("call_control_id", ""))
        if ccid:
            self._remember(self.outbound, ccid, meta)
        return {"ok": True, "call": data, "via": "telnyx"}


    # -- carrier webhook -------------------------------------------------------------
    async def handle_webhook(self, payload: dict[str, Any]) -> dict[str, Any]:
        from xplogent.voip.service import is_outbound

        cfg = load_config()
        v = cfg.voip or {}
        ev = parse_webhook(payload)
        self._expire()
        ccid = ev["call_control_id"]
        xfer_of = ev["client_state"][5:] if ev["client_state"].startswith("xfer:") else ""
        self._log_event(xfer_of or ccid, ev, leg="transfer" if xfer_of else "")
        ours = ccid in self.outbound or bool(xfer_of) or self.service.live_by_ref(ccid) is not None
        # Switched off: answer nothing new — but calls we placed (test calls) and calls
        # already in progress still get their events.
        if not v.get("enabled") and not ours:
            return {"ok": False, "error": "voip is disabled"}
        if xfer_of:                  # an event on the new leg of one of our transfers
            live = self.service.live_by_ref(xfer_of)
            if live is not None and ev["event"] == "call.bridged":
                live.session.transfer_done()
            elif live is not None and ev["event"] == "call.hangup":
                self._spawn(live.session.transfer_failed("the other side didn't answer"))
            return {"ok": True}
        if ev["event"] == "call.dtmf.received":      # the one source of keypad presses
            live = self.service.live_by_ref(ccid)
            if live is not None and ev["digit"]:
                self._spawn(live.session.on_dtmf(ev["digit"]))
            return {"ok": True}
        if ev["event"] == "call.bridged":
            live = self.service.live_by_ref(ccid)
            if live is not None and live.session.transfer_pending:
                live.session.transfer_done()
            return {"ok": True}

        def client() -> TelnyxClient:
            return self._client(v)

        if ev["event"] == "call.initiated":
            self._remember(self.by_call, ccid, ev)
            if not is_outbound(ev):
                from xplogent.voip import crm

                if crm.lookup_enabled(cfg) and ev.get("from"):   # runs while the call connects
                    while len(self.lookups) >= 50:
                        self.lookups.pop(next(iter(self.lookups))).cancel()
                    self.lookups[ccid] = asyncio.create_task(
                        crm.lookup(cfg, ev["from"], direction="inbound", line="telnyx"))
                c = client()
                self._spawn(self._act(c, c.answer(ccid)))
        elif ev["event"] == "call.answered":
            info = {**self.by_call.get(ccid, ev), "call_control_id": ccid, "transport": "telnyx",
                    **self.outbound.pop(ccid, {})}
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
        # Looked up at call time (tests replace them on the service module).
        from xplogent.voip import service as svc

        build_session, finalize_call = svc.build_session, svc.finalize_call
        customer_number, is_outbound = svc.customer_number, svc.is_outbound
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
        client = self._client(v)
        ccid = info.get("call_control_id", "")
        stream_id = {"id": ""}

        media = {"frames_in": 0, "frames_out": 0}

        async def send_audio(chunk: bytes) -> None:
            media["frames_out"] += 1
            await websocket.send_text(media_message(chunk))

        async def clear_audio() -> None:
            await websocket.send_text(clear_message())

        async def hangup() -> None:
            await client.hangup(ccid)

        async def transfer_to(number: str) -> None:
            target = resolve_transfer_number(v, number, transport="telnyx")
            if not target:
                raise ValueError("no valid transfer number")
            await client.transfer(ccid, target, client_state=f"xfer:{ccid}")

        async def transfer(reason: str) -> None:      # the agent decided to hand over
            await transfer_to("")

        async def send_dtmf(digits: str) -> None:
            await client.send_dtmf(ccid, digits)

        session = brain = None
        call_id = 0
        lookup_task = self.lookups.pop(ccid, None)
        if lookup_task is not None:
            with contextlib.suppress(Exception):
                info = {**info, "crm_lookup": await lookup_task}   # bounded by the lookup's timeout
        try:
            # Inside the try: if building the session fails, the store, the Telnyx client
            # and the socket are still closed below.
            session, call_id, brain = build_session(
                cfg, store, info, send_audio=send_audio, clear_audio=clear_audio,
                hangup=hangup, transfer=transfer, send_dtmf=send_dtmf)
            self.service.register(LiveCall(
                call_id=call_id, session=session, brain=brain, number=customer_number(info),
                direction="outbound" if is_outbound(info) else "inbound", transport="telnyx",
                test=bool(info.get("test")), contact_name=str((brain.contact or {}).get("name") or ""),
                transfer_to=transfer_to, ref=ccid))
            began = session.started

            def telnyx_debug() -> dict[str, Any]:
                return {"telnyx_events": [{**e, "t": round(e["at"] - began, 2)}
                                          for e in self.call_events.get(ccid, [])],
                        "media": dict(media, stream_id=stream_id["id"])}

            session.debug_sources.append(telnyx_debug)
            started = False
            while not session.finished.is_set():
                try:
                    raw = await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
                except TimeoutError:
                    continue
                msg = parse_stream_message(raw)
                if msg["event"] == "start":
                    stream_id["id"] = msg["stream_id"]
                # Speak once the stream has started; some carriers skip `start`, so the
                # first media frame counts too.
                if not started and msg["event"] in ("start", "media"):
                    started = True
                    await session.start()
                if msg["event"] == "media":
                    media["frames_in"] += 1
                    await session.on_audio(msg["audio"])
                elif msg["event"] == "stop":
                    break
        except Exception as exc:  # noqa: BLE001 - classify, log, then clean up below
            if type(exc).__name__ in ("WebSocketDisconnect", "ClientDisconnected"):
                _log.info("call %s: media stream closed by the carrier", call_id or ccid)
            else:
                _log.exception("call %s: media stream failed", call_id or ccid)
        finally:
            if call_id:
                self.service.unregister(call_id)
            closers = [client.aclose]
            if session is not None and brain is not None:
                await session.stop()
                result = finalize_call(cfg, store, call_id, session, brain, customer_number(info))
                _log.info("call %s finished: %s", call_id, result)
                self.call_events.pop(ccid, None)
                closers += [session.stt.aclose, session.tts.aclose, brain.provider.aclose]
                if hasattr(brain.retrieve, "aclose"):
                    closers.append(brain.retrieve.aclose)
            for closer in closers:
                with contextlib.suppress(Exception):
                    await closer()
            store.close()
            with contextlib.suppress(Exception):
                await websocket.close()
