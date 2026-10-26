"""Realtime speech-to-speech calls (``voip.mode: realtime``), OpenAI Realtime first.

``RealtimeSession`` *is* a :class:`CallSession`: live control, transfers, recording,
the timeline and debug view, keypad, CRM and finalize all work unchanged. Only the
audio path differs — the caller's G.711 μ-law goes straight to the model (no
resampling, no local speech-to-text) and the model's voice streams back, paced to
playback speed. The model hears and speaks; Xplogent keeps the rules:

- the greeting (with the AI and recording disclosure) is still spoken word for word
  by the configured voice, and the caller cannot cut it off;
- tools run through the same ``CallBrain`` handlers, so the mandatory-field guard
  still refuses ``end_call``, and do-not-call / transfer / callback behave the same;
  knowledge becomes a ``search_knowledge`` tool;
- the model only speaks when we ask (``create_response: false``), so a supervisor's
  "Pause AI" really pauses it;
- if the realtime connection can't be made, the call runs in the normal (cascaded)
  mode instead.

Incoming events are accepted under both the GA names (``response.output_audio.*``)
and the earlier beta names (``response.audio.*``).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import time
from typing import Any

from xplogent.core.logging import get_logger
from xplogent.voip.engine import MULAW_FRAME, CallSession

_log = get_logger("voip.realtime")

SEARCH_TOOL = {"type": "function", "name": "search_knowledge",
               "description": "Look up facts in the company's knowledge base (prices, hours, policies, "
                              "products) before answering a question about them.",
               "parameters": {"type": "object", "properties": {"query": {"type": "string"}},
                              "required": ["query"]}}
_AUDIO_DELTA = {"response.output_audio.delta", "response.audio.delta"}
_AGENT_TEXT = {"response.output_audio_transcript.done", "response.audio_transcript.done"}


class RealtimeSession(CallSession):
    def __init__(self, *args: Any, rt_url: str, rt_api_key: str = "", rt_model: str = "gpt-realtime",
                 rt_voice: str = "marin", rt_transcription_model: str = "gpt-4o-transcribe",
                 connect_timeout_s: float = 5.0, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.rt_url, self.rt_api_key, self.rt_model = rt_url, rt_api_key, rt_model
        self.rt_voice, self.rt_transcription_model = rt_voice, rt_transcription_model
        self.connect_timeout_s = connect_timeout_s
        self.mode = "realtime"
        self._ws: Any = None
        self._reader: asyncio.Task | None = None
        self._player: asyncio.Task | None = None
        self._play: asyncio.Queue[bytes] = asyncio.Queue()
        self._model_speaking = False
        self._speech_stopped_at = 0.0
        self._first_audio_pending = False
        self._agent_line = ""

    # -- lifecycle -----------------------------------------------------------------------
    async def start(self) -> None:
        try:
            await self._connect()
        except Exception as exc:  # noqa: BLE001 - fall back to the cascaded engine
            _log.warning("realtime connection failed (%s): this call uses the normal mode", exc)
            self.mode = "cascaded"
            self._ws = None
            self.event("realtime_unavailable", f"{type(exc).__name__}: {exc}"[:160])
        else:
            self.event("realtime", self.rt_model)
        await super().start()

    async def _connect(self) -> None:
        from websockets.asyncio.client import connect

        headers = {"Authorization": f"Bearer {self.rt_api_key}"} if self.rt_api_key else {}
        self._ws = await asyncio.wait_for(
            connect(self.rt_url, additional_headers=headers, max_size=None, close_timeout=1),
            timeout=self.connect_timeout_s)
        await self._send({"type": "session.update", "session": self._session_config()})
        self._reader = asyncio.create_task(self._read())
        self._player = asyncio.create_task(self._play_loop())

    def _session_config(self) -> dict[str, Any]:
        tools = [{"type": "function", "name": t.name, "description": t.description,
                  "parameters": t.parameters} for t in self.brain.tools]
        if self.brain.retrieve is not None:
            tools.append(SEARCH_TOOL)
        return {"type": "realtime", "model": self.rt_model, "instructions": self._instructions(),
                "audio": {"input": {"format": {"type": "audio/pcmu"},
                                    "transcription": {"model": self.rt_transcription_model},
                                    "turn_detection": {"type": "server_vad", "create_response": False,
                                                       "interrupt_response": True}},
                          "output": {"format": {"type": "audio/pcmu"}, "voice": self.rt_voice}},
                "tools": tools, "tool_choice": "auto"}

    def _instructions(self) -> str:
        hint = ["(use the search_knowledge tool to look facts up before answering)"] \
            if self.brain.retrieve is not None else []
        return (self.brain._system(hint) + "\n\nYou are speaking on a phone call. The greeting has "
                "already been said: do not greet again. Keep replies short and natural.")

    async def stop(self) -> None:
        await super().stop()
        for task in (self._reader, self._player):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()

    async def _send(self, event: dict[str, Any]) -> None:
        if self._ws is not None:
            await self._ws.send(json.dumps(event))

    # -- caller audio -----------------------------------------------------------------------
    async def on_audio(self, mulaw: bytes) -> None:
        if self._ws is None:
            return await super().on_audio(mulaw)
        if self.finished.is_set():
            return
        if self.recorder is not None:
            self.recorder.caller(mulaw)
        with contextlib.suppress(Exception):
            await self._send({"type": "input_audio_buffer.append", "audio": base64.b64encode(mulaw).decode()})

    async def _reply(self, text: str, record_as: str | None = None) -> None:
        """Typed text (browser tests) and keypad digits: a user message to the model."""
        if self._ws is None:
            return await super()._reply(text, record_as)
        if len(text) < 2 and record_as is None:
            return
        self._last_activity = time.time()
        self._record("caller", record_as or text)
        if getattr(self.brain, "paused", False):
            return
        self._speech_stopped_at = time.time()
        await self._send({"type": "conversation.item.create", "item": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}})
        await self._respond()

    async def _respond(self) -> None:
        if getattr(self.brain, "paused", False) or self.finished.is_set() or self.transfer_pending:
            return
        await self._send({"type": "session.update", "session": {"type": "realtime",
                                                                "instructions": self._instructions()}})
        self._first_audio_pending = True
        await self._send({"type": "response.create"})

    # -- model events ------------------------------------------------------------------------
    async def _read(self) -> None:
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    continue
                try:
                    await self._on_event(json.loads(raw))
                except Exception:  # noqa: BLE001 - one bad event must not end the call
                    _log.exception("realtime event failed")
        except Exception as exc:  # noqa: BLE001
            if not self.finished.is_set():
                _log.warning("realtime connection lost: %s", exc)
        if not self.finished.is_set():
            self.event("realtime_lost")
            await self._hangup("the realtime connection was lost")

    async def _on_event(self, ev: dict[str, Any]) -> None:
        kind = ev.get("type", "")
        if kind == "input_audio_buffer.speech_started":
            self._last_activity = time.time()
            self._idle_prompts = 0
            if self._model_speaking or self._speaking:
                await self._barge_in()
        elif kind == "input_audio_buffer.speech_stopped":
            self._speech_stopped_at = time.time()
        elif kind == "conversation.item.input_audio_transcription.completed":
            text = str(ev.get("transcript", "")).strip()
            if text:
                self._last_activity = time.time()
                self._record("caller", text)
                await self._after_greeting()
                await self._respond()
        elif kind in _AUDIO_DELTA:
            chunk = base64.b64decode(ev.get("delta", "") or "")
            if chunk:
                if self._first_audio_pending:
                    self._first_audio_pending = False
                    total = (time.time() - self._speech_stopped_at) * 1000 if self._speech_stopped_at else 0.0
                    self.latencies_ms.append(total)
                    self.turn_timings.append({"total_ms": round(total, 1), "stt": "realtime",
                                              "llm": f"realtime:{self.rt_model}",
                                              "tts": f"realtime:{self.rt_voice}", "fallbacks": [], "error": ""})
                self._model_speaking = True
                await self._play.put(chunk)
        elif kind in _AGENT_TEXT:
            text = str(ev.get("transcript", "")).strip()
            if text:
                self._record("agent", text)
                if hasattr(self.brain, "said"):
                    self.brain.said(text)
        elif kind == "response.function_call_arguments.done":
            await self._tool(ev)
        elif kind == "response.done":
            self._last_activity = time.time()
            await self._after_turn()
        elif kind == "error":
            err = (ev.get("error") or {}).get("message", "") if isinstance(ev.get("error"), dict) else str(ev)
            self.event("realtime_error", err)
            self.turn_timings.append({"stt": "realtime", "llm": f"realtime:{self.rt_model}",
                                      "tts": f"realtime:{self.rt_voice}", "fallbacks": [], "error": err[:300]})

    async def _tool(self, ev: dict[str, Any]) -> None:
        name = str(ev.get("name", ""))
        try:
            args = json.loads(ev.get("arguments") or "{}")
        except ValueError:
            args = {}
        if name == "search_knowledge" and self.brain.retrieve is not None:
            try:
                hits = await self.brain.retrieve(str(args.get("query", "")))
                result = "\n---\n".join(hits) if hits else "(nothing relevant found)"
            except Exception:  # noqa: BLE001
                result = "(the knowledge base is unavailable right now)"
        else:
            result = self.brain._run_tool(name, args)
            self.brain.state.events.append(f"{name}: {result}")
        self.event("tool", f"{name}: {result[:80]}")
        await self._send({"type": "conversation.item.create", "item": {
            "type": "function_call_output", "call_id": ev.get("call_id", ""), "output": result[:4000]}})
        st = self.brain.state
        if not (st.transfer or st.end):                 # let the model carry on after the tool
            await self._respond()

    # -- speaking ---------------------------------------------------------------------------------
    async def _play_loop(self) -> None:
        """Model audio to the carrier at playback speed (barge-in can drop what's queued)."""
        loop = asyncio.get_running_loop()
        play_end = loop.time()
        carry = b""
        while True:
            chunk = await self._play.get()
            carry += chunk
            while len(carry) >= MULAW_FRAME:
                frame, carry = carry[:MULAW_FRAME], carry[MULAW_FRAME:]
                ahead = play_end - loop.time()
                if self.realtime and ahead > self.lead_s:
                    await asyncio.sleep(ahead - self.lead_s)
                await self.send_audio(frame)
                if self.recorder is not None:
                    self.recorder.agent(frame)
                play_end = max(play_end, loop.time()) + MULAW_FRAME / 8000.0
            if self._play.empty():
                if self.realtime:
                    await asyncio.sleep(max(0.0, play_end - loop.time()))
                if self._play.empty():
                    self._model_speaking = False
                    self._last_activity = time.time()

    async def _barge_in(self) -> None:
        if self._ws is None:
            return await super()._barge_in()
        if self._greeting_playing():
            return
        self.event("barge_in")
        while not self._play.empty():
            self._play.get_nowait()
        self._model_speaking = False
        with contextlib.suppress(Exception):
            await self._send({"type": "response.cancel"})
        if self.clear_audio:
            with contextlib.suppress(Exception):
                await self.clear_audio()
        await super()._barge_in()
