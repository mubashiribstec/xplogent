"""VoipService — wires carrier webhooks and media sockets to CallSession + persistence.

``build_session`` is the testable core: give it config, a store, and (optionally) fake
STT/TTS/LLM and it returns a ready :class:`CallSession` and the new call row id. The
service class adds the real-world plumbing (answer the call, open the stream, run the
socket loop, wrap up the call record, honour do-not-call).
"""

from __future__ import annotations

import asyncio
import contextlib
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


def build_session(cfg: Config, store: Store, info: dict[str, str], *, send_audio, clear_audio=None,
                  hangup=None, transfer=None, stt=None, tts=None, provider=None,
                  retrieve=None) -> tuple[CallSession, int, CallBrain]:
    """Create a CallSession + its call row. Injected parts make this unit-testable."""
    v = cfg.voip or {}
    caller = info.get("from", "")
    contact = store.find_contact_by_phone(caller) if caller else None
    direction = "outbound" if info.get("direction") == "outgoing" else "inbound"
    call_id = store.start_call(direction, caller, info.get("to", ""),
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


def _make_retriever(cfg: Config, store: Store, k: int):
    from xplogent.core.rag import hybrid_search
    from xplogent.memory.vector import Embedder
    from xplogent.providers.registry import build_provider

    embedder = Embedder(build_provider(cfg.embedding_model))

    async def retrieve(query: str) -> list[str]:
        hits = await hybrid_search(store, embedder, query, k=k)
        return [h["content"] for h in hits]

    return retrieve


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
    def __init__(self) -> None:
        self.pending: dict[str, dict[str, str]] = {}      # stream token -> call info
        self.by_call: dict[str, dict[str, str]] = {}      # call_control_id -> info
        self.active: dict[int, dict[str, Any]] = {}       # call row id -> live info
        self._tasks: set[asyncio.Task] = set()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- carrier webhook -------------------------------------------------------------
    async def handle_webhook(self, payload: dict[str, Any]) -> dict[str, Any]:
        cfg = load_config()
        v = cfg.voip or {}
        if not v.get("enabled"):
            return {"ok": False, "error": "voip is disabled"}
        ev = parse_webhook(payload)
        client = TelnyxClient(api_key_env=str(v.get("api_key_env", "TELNYX_API_KEY")))
        if ev["event"] == "call.initiated" and ev["direction"] in ("incoming", "inbound"):
            self.by_call[ev["call_control_id"]] = ev
            self._spawn(self._act(client, client.answer(ev["call_control_id"])))
        elif ev["event"] == "call.answered":
            info = self.by_call.get(ev["call_control_id"], ev)
            token = uuid.uuid4().hex
            self.pending[token] = {**info, "call_control_id": ev["call_control_id"]}
            base = str(v.get("public_url", "")).rstrip("/")
            ws_url = base.replace("https://", "wss://").replace("http://", "ws://") + f"/ws/telephony/{token}"
            self._spawn(self._act(client, client.start_stream(ev["call_control_id"], ws_url)))
        elif ev["event"] == "call.hangup":
            self.by_call.pop(ev["call_control_id"], None)
        else:
            await client.aclose()
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
        info = self.pending.pop(token, None)
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
        self.active[call_id] = {"from": info.get("from", ""), "started": session.started}
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
            result = finalize_call(cfg, store, call_id, session, brain, info.get("from", ""))
            _log.info("call %s finished: %s", call_id, result)
            for closer in (session.stt.aclose(), session.tts.aclose(), client.aclose(),
                           brain.provider.aclose()):
                with contextlib.suppress(Exception):
                    await closer
            store.close()
            with contextlib.suppress(Exception):
                await websocket.close()


_SERVICE: VoipService | None = None


def get_service() -> VoipService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = VoipService()
    return _SERVICE
