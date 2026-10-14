"""Test calls in the browser: the real call pipeline with the browser as the phone.

The dashboard opens ``/ws/voip/test`` and talks to the same :class:`CallSession` and
:class:`CallBrain` a phone call uses (persona, playbook, knowledge, tools), so what
you hear is what a caller would hear — only the phone line is replaced:

* browser → server: JSON ``{"type": "text", "text": …}`` for typed caller turns,
  binary frames of 8 kHz μ-law for microphone audio (HTTPS only), and
  ``{"type": "hangup"}``;
* server → browser: JSON ``turn`` / ``events`` / ``clear`` / ``transfer`` /
  ``ended`` messages and binary 8 kHz μ-law audio frames of the agent's voice.

The call is stored with ``test`` set, so it is not counted and never changes a
contact's status or the do-not-call list.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

from xplogent.core.config import Config, load_config
from xplogent.core.logging import get_logger
from xplogent.memory.store import Store
from xplogent.voip.control import LiveCall

_log = get_logger("voip.testcall")


class SilentTTS:
    """Text-only test calls (no speech server needed)."""

    sample_rate = 8000

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        if False:  # pragma: no cover - an empty async generator
            yield b""

    async def aclose(self) -> None:
        return None


async def run_test_call(websocket, cfg: Config | None = None, *, stt=None, tts=None,
                        provider=None, retrieve=None, service=None) -> None:
    """Drive one browser test call over an accepted WebSocket."""
    from xplogent.voip.service import build_session, finalize_call, get_service

    cfg = cfg or load_config()
    service = service or get_service()
    try:
        start = json.loads(await asyncio.wait_for(websocket.receive_text(), timeout=30))
    except Exception:  # noqa: BLE001
        await websocket.close(code=1008)
        return
    voice = bool(start.get("voice", True))
    v = cfg.voip or {}
    outbox: asyncio.Queue = asyncio.Queue()

    async def pump() -> None:
        while True:
            item = await outbox.get()
            if item is None:
                return
            with contextlib.suppress(Exception):
                if isinstance(item, bytes):
                    await websocket.send_bytes(item)
                else:
                    await websocket.send_text(json.dumps(item))

    sender = asyncio.create_task(pump())
    store = Store(cfg.db_path)

    async def send_audio(frame: bytes) -> None:
        if voice:
            outbox.put_nowait(bytes(frame))

    async def clear_audio() -> None:
        outbox.put_nowait({"type": "clear"})

    async def hangup() -> None:
        outbox.put_nowait({"type": "hangup"})

    def simulate_transfer(number: str, reason: str) -> None:
        # No real phone line: report where the call would go, and end like a
        # successful transfer does.
        outbox.put_nowait({"type": "transfer", "to": number, "reason": reason})
        if session is not None:
            session.transfer_done()

    async def transfer(reason: str) -> None:
        simulate_transfer(str(v.get("transfer_number", "")), reason)

    async def on_events(events: list[str]) -> None:
        if events:
            outbox.put_nowait({"type": "events", "items": events})

    def on_turn(call_id_box: list[int], speaker: str, text: str) -> None:
        if call_id_box:
            store.add_call_turn(call_id_box[0], speaker, text)
        outbox.put_nowait({"type": "turn", "speaker": speaker, "text": text})

    contact_id = start.get("contact_id")
    info: dict[str, Any] = {"direction": "inbound", "from": "browser test", "to": "",
                            "test": True, "transport": "browser", "direction_label": "test",
                            "contact_id": int(contact_id) if str(contact_id or "").isdigit() else None}
    box: list[int] = []
    session = brain = None
    call_id = 0
    try:
        session, call_id, brain = build_session(
            cfg, store, info, send_audio=send_audio, clear_audio=clear_audio, hangup=hangup,
            transfer=transfer, stt=stt, tts=tts if tts is not None else (None if voice else SilentTTS()),
            provider=provider, retrieve=retrieve, on_events=on_events,
            idle_prompt_s=300.0, idle_hangup_s=900.0)
        box.append(call_id)
        session.on_turn = lambda speaker, text: on_turn(box, speaker, text)
        outbox.put_nowait({"type": "started", "call_id": call_id, "voice": voice,
                           "contact": (brain.contact or {}).get("name") or ""})

        async def transfer_to(number: str) -> None:
            simulate_transfer(number, "from the dashboard")

        service.register(LiveCall(call_id=call_id, session=session, brain=brain, number="browser test",
                                  direction="test", transport="browser", test=True,
                                  contact_name=str((brain.contact or {}).get("name") or ""),
                                  transfer_to=transfer_to))
        await session.start()
        while not session.finished.is_set():
            try:
                msg = await asyncio.wait_for(websocket.receive(), timeout=1.0)
            except TimeoutError:
                continue
            if msg.get("type") == "websocket.disconnect":
                break
            if msg.get("bytes"):
                await session.on_audio(msg["bytes"])
                continue
            try:
                data = json.loads(msg.get("text") or "{}")
            except ValueError:
                continue
            if data.get("type") == "text":
                await session.on_text(str(data.get("text", ""))[:2000])
            elif data.get("type") == "hangup":
                break
        # Let the last words reach the browser before closing.
        if session._turn_task is not None and not session._turn_task.done():
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(session._turn_task), timeout=20)
    except Exception as exc:  # noqa: BLE001 - shown in the test panel
        _log.exception("test call failed")
        outbox.put_nowait({"type": "error", "error": str(exc)[:300]})
    finally:
        if call_id:
            service.unregister(call_id)
        result: dict[str, Any] = {}
        if session is not None and brain is not None:
            await session.stop()
            result = finalize_call(cfg, store, call_id, session, brain)
            st = brain.state
            outbox.put_nowait({"type": "ended", "outcome": result.get("outcome", ""),
                               "summary": st.summary, "callback": st.callback,
                               "latency_ms": [round(x) for x in result.get("latency_ms", [])]})
            closers = [session.stt.aclose, session.tts.aclose, brain.provider.aclose]
            if hasattr(brain.retrieve, "aclose"):
                closers.append(brain.retrieve.aclose)
            for closer in closers:
                with contextlib.suppress(Exception):
                    await closer()
        outbox.put_nowait(None)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(sender, timeout=5)
        store.close()
        with contextlib.suppress(Exception):
            await websocket.close()
