"""Deepgram Voice Agent calls (``voip.mode: voice_agent``).

One WebSocket to Deepgram does the listening (Nova-3), the thinking (a Deepgram-hosted
model: OpenAI, Anthropic or Google) and the speaking (an Aura-2 voice, or a Cartesia voice
with your Cartesia key). The caller's G.711 μ-law goes straight in and the agent's μ-law
comes straight back, so there is no separate speech-to-text or text-to-speech to choose.

``DeepgramAgentSession`` *is* a :class:`CallSession` (through the OpenAI realtime engine,
whose playback, recording, timeline and fallback it shares): live control, transfers,
the keypad, the CRM and finalize work unchanged. Xplogent keeps the rules:

- the greeting (with the AI and recording disclosure) is the agent's ``greeting`` and is
  recorded word for word; speech over it never cuts off what has already arrived;
- tools run through the same ``CallBrain`` handlers (client-side function calls), so the
  mandatory-field guard still refuses ``end_call``; knowledge is a ``search_knowledge`` tool;
- "Pause AI" stops sending the caller's audio to the agent, so it cannot answer;
- if Deepgram can't be reached or refuses the settings, the call runs in the normal
  (standard) mode instead.

Not available in this mode (the model's words go straight to its voice): the pay guard,
local answering-machine detection, voicemail drop and listen-first.

Protocol: ``wss://agent.deepgram.com/v1/agent/converse``, ``Authorization: Token <key>``;
``Welcome`` → ``Settings`` → ``SettingsApplied``; binary audio both ways; events
``ConversationText``, ``UserStartedSpeaking``, ``AgentThinking``, ``FunctionCallRequest``,
``AgentStartedSpeaking``, ``AgentAudioDone``, ``Error``, ``Warning``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from typing import Any

from xplogent.core.logging import get_logger
from xplogent.voip.engine import CallSession
from xplogent.voip.realtime import SEARCH_TOOL, RealtimeSession

_log = get_logger("voip.voice_agent")

DEFAULT_URL = "wss://agent.deepgram.com/v1/agent/converse"
MODELS_URL = "https://agent.deepgram.com/v1/agent/settings/think/models"
GREETING_MAX_S = 20.0           # stop holding barge-in for the greeting after this
PROMPT_LIMIT = 24_000            # Deepgram-hosted models take up to 25,000 characters
KEEPALIVE_S = 8.0

# Deepgram-hosted models (developers.deepgram.com/docs/voice-agent-llm-models), used when
# the live list can't be fetched. Fast, tool-capable ones first.
FALLBACK_THINK_MODELS: list[dict[str, str]] = [
    {"provider": "open_ai", "model": "gpt-4o-mini", "name": "OpenAI GPT-4o mini"},
    {"provider": "open_ai", "model": "gpt-4.1-mini", "name": "OpenAI GPT-4.1 mini"},
    {"provider": "open_ai", "model": "gpt-4.1-nano", "name": "OpenAI GPT-4.1 nano"},
    {"provider": "open_ai", "model": "gpt-5-mini", "name": "OpenAI GPT-5 mini"},
    {"provider": "open_ai", "model": "gpt-4o", "name": "OpenAI GPT-4o"},
    {"provider": "anthropic", "model": "claude-haiku-4-5", "name": "Anthropic Claude Haiku 4.5"},
    {"provider": "anthropic", "model": "claude-sonnet-4-6", "name": "Anthropic Claude Sonnet 4.6"},
    {"provider": "google", "model": "gemini-2.5-flash", "name": "Google Gemini 2.5 Flash"},
    {"provider": "google", "model": "gemini-3.1-flash-lite", "name": "Google Gemini 3.1 Flash Lite"},
]
THINK_PROVIDERS = ("open_ai", "anthropic", "google")


# The voices the Voice Agent can speak with (developers.deepgram.com/docs/voice-agent-tts-models).
# Deepgram's own (Aura-2, Flux, Aura) need no other key; the rest use your key for that provider,
# which travels to Deepgram in the session settings (Deepgram calls the provider for you).
VOICE_PROVIDERS = ("deepgram", "flux", "aura", "cartesia", "elevenlabs", "openai")
ELEVENLABS_MODELS = ("eleven_turbo_v2_5", "eleven_v3")
OPENAI_TTS_MODELS = ("tts-1", "tts-1-hd")
_KEY_FOR = {"cartesia": "CARTESIA_API_KEY", "elevenlabs": "ELEVENLABS_API_KEY", "openai": "OPENAI_API_KEY"}


def voice_provider_of(va: dict[str, Any]) -> str:
    p = str(va.get("voice_provider") or "deepgram")
    return p if p in VOICE_PROVIDERS else "deepgram"


def voice_key_env(va: dict[str, Any]) -> str:
    """The extra key the chosen voice needs ("" for Deepgram's own voices)."""
    return _KEY_FOR.get(voice_provider_of(va), "")


def _speed(va: dict[str, Any], lo: float, hi: float) -> float | None:
    raw = va.get("speed")
    if raw in (None, ""):
        return None
    try:
        return round(max(lo, min(float(raw), hi)), 2)
    except (TypeError, ValueError):
        return None


def chosen_voice(va: dict[str, Any]) -> str:
    """The voice id for the chosen provider (with each provider's default)."""
    from xplogent.voip.providers import (
        AURA_V1_DEFAULT_VOICE,
        DEEPGRAM_DEFAULT_VOICE,
        FLUX_DEFAULT_VOICE,
    )

    p = voice_provider_of(va)
    return str({"deepgram": va.get("voice") or DEEPGRAM_DEFAULT_VOICE,
                "flux": va.get("flux_voice") or FLUX_DEFAULT_VOICE,
                "aura": va.get("aura_voice") or AURA_V1_DEFAULT_VOICE,
                "cartesia": va.get("cartesia_voice") or "",
                "elevenlabs": va.get("elevenlabs_voice") or "",
                "openai": va.get("openai_voice") or "alloy"}[p])


def speak_provider(va: dict[str, Any], env: dict[str, str]) -> dict[str, Any] | list[dict[str, Any]]:
    """``agent.speak`` for the chosen voice. A third-party voice is followed by a Deepgram
    Aura-2 voice: Deepgram tries them in order, so a refused key never leaves the call mute."""
    from xplogent.voip.providers import CARTESIA_API_VERSION, CARTESIA_MODEL, DEEPGRAM_DEFAULT_VOICE

    p, voice = voice_provider_of(va), chosen_voice(va)
    lang = str(va.get("language") or "en")
    backup = {"provider": {"type": "deepgram", "model": str(va.get("voice") or DEEPGRAM_DEFAULT_VOICE)}}
    if p == "deepgram":
        prov: dict[str, Any] = {"type": "deepgram", "model": voice}
        if (sp := _speed(va, 0.7, 1.5)) is not None:
            prov["speed"] = sp
        return {"provider": prov}
    if p == "flux":
        prov = {"type": "deepgram", "version": "v2", "model": voice}
        if (sp := _speed(va, 0.5, 1.5)) is not None:
            prov["speed"] = sp
        if va.get("expressivity") not in (None, ""):
            try:
                prov["expressivity"] = max(-2, min(int(va["expressivity"]), 2))
            except (TypeError, ValueError):
                pass
        return {"provider": prov}
    if p == "aura":
        return {"provider": {"type": "deepgram", "version": "v1", "model": voice}}
    if p == "cartesia":
        prov = {"type": "cartesia", "model_id": CARTESIA_MODEL, "voice": {"mode": "id", "id": voice},
                "language": lang}
        if (sp := _speed(va, 0.6, 1.5)) is not None:
            prov["speed"] = sp
        main = {"provider": prov,
                "endpoint": {"url": "https://api.cartesia.ai/tts/bytes",
                             "headers": {"x-api-key": env.get("CARTESIA_API_KEY", ""),
                                         "cartesia-version": CARTESIA_API_VERSION}}}
    elif p == "elevenlabs":
        model = str(va.get("elevenlabs_model") or ELEVENLABS_MODELS[0])
        model = model if model in ELEVENLABS_MODELS else ELEVENLABS_MODELS[0]
        url = (f"https://api.elevenlabs.io/v1/text-to-speech/{voice}" if model == "eleven_v3" else
               f"wss://api.elevenlabs.io/v1/text-to-speech/{voice}/multi-stream-input")
        main = {"provider": {"type": "eleven_labs", "model_id": model,
                             "language_code": "en-US" if lang == "en" else lang},
                "endpoint": {"url": url, "headers": {"xi-api-key": env.get("ELEVENLABS_API_KEY", "")}}}
    else:  # openai
        model = str(va.get("openai_model") or OPENAI_TTS_MODELS[0])
        main = {"provider": {"type": "open_ai", "model": model if model in OPENAI_TTS_MODELS else "tts-1",
                             "voice": voice},
                "endpoint": {"url": "https://api.openai.com/v1/audio/speech",
                             "headers": {"authorization": f"Bearer {env.get('OPENAI_API_KEY', '')}"}}}
    return [main, backup]


def _same_words(a: str, b: str) -> bool:
    def words(s: str) -> list[str]:
        return re.findall(r"[a-z0-9']+", (s or "").lower())

    wa, wb = words(a), words(b)
    return bool(wa) and wa == wb


def think_provider(va: dict[str, Any]) -> dict[str, Any]:
    think = va.get("think") or {}
    ptype = str(think.get("provider") or "open_ai")
    if ptype not in THINK_PROVIDERS:
        ptype = "open_ai"
    out: dict[str, Any] = {"type": ptype, "model": str(think.get("model") or "gpt-4o-mini")}
    if think.get("temperature") is not None:
        temp = float(think["temperature"])
        out["temperature"] = max(0.0, min(temp, 1.0 if ptype == "anthropic" else 2.0))
    return out


def voice_label(va: dict[str, Any]) -> str:
    return f"{voice_provider_of(va)}:{chosen_voice(va) or '?'}"


class DeepgramAgentSession(RealtimeSession):
    def __init__(self, *args: Any, va_url: str = DEFAULT_URL, va_api_key: str = "",
                 va_settings: dict[str, Any] | None = None, connect_timeout_s: float = 5.0,
                 **kwargs: Any) -> None:
        self.va = dict(va_settings or {})
        think = think_provider(self.va)
        super().__init__(*args, rt_url=va_url, rt_api_key=va_api_key,
                         rt_model=f"{think['type']}:{think['model']}", rt_voice=voice_label(self.va),
                         connect_timeout_s=connect_timeout_s, **kwargs)
        self.mode = "voice_agent"
        self._keepalive: asyncio.Task | None = None
        self._greeting_live = False          # the agent's greeting is still arriving / playing
        self._greeting_text_seen = False
        self._greeting_guard: asyncio.Task | None = None
        self._user_said_at = 0.0

    # -- lifecycle -------------------------------------------------------------------------
    async def start(self) -> None:
        try:
            await self._connect()
        except Exception as exc:  # noqa: BLE001 - fall back to the standard engine
            _log.warning("Deepgram Voice Agent unavailable (%s): this call uses the standard mode", exc)
            self.mode = "cascaded"
            self._ws = None
            self.event("voice_agent_unavailable", f"{type(exc).__name__}: {exc}"[:160])
            await CallSession.start(self)
            return
        self.event("voice_agent", f"{self.rt_model} · {self.rt_voice}")
        self._watchdog = asyncio.create_task(self._watch())
        self.event("answered")
        if self.greeting:
            # Deepgram speaks it (agent.greeting) in the agent's own voice; recorded here.
            self.event("greeting")
            self._record("agent", self.greeting)
            self._greeting_live = True
            self._greeting_guard = asyncio.create_task(self._greeting_timeout())

    async def _greeting_timeout(self, seconds: float = GREETING_MAX_S) -> None:
        """Barge-in is held while the greeting (with the AI disclosure) plays. If Deepgram never
        says the greeting is done (an interrupted greeting, a glitch), stop holding it."""
        await asyncio.sleep(seconds)
        if self._greeting_live:
            self._greeting_live = False
            self.event("greeting_timeout")

    async def _connect(self) -> None:
        from websockets.asyncio.client import connect

        headers = {"Authorization": f"Token {self.rt_api_key}"} if self.rt_api_key else {}
        ws = await asyncio.wait_for(
            connect(self.rt_url, additional_headers=headers, max_size=None, close_timeout=1),
            timeout=self.connect_timeout_s)
        try:
            async def handshake() -> None:
                sent = False
                async for raw in ws:
                    if isinstance(raw, bytes):
                        continue
                    msg = json.loads(raw)
                    kind = msg.get("type")
                    if kind == "Welcome" and not sent:
                        await ws.send(json.dumps(self.settings()))
                        sent = True
                    elif kind == "SettingsApplied":
                        return
                    elif kind == "Error":
                        raise RuntimeError(str(msg.get("description") or msg.get("code") or "settings refused"))
                raise RuntimeError("the connection closed before the settings were accepted")

            await asyncio.wait_for(handshake(), timeout=self.connect_timeout_s)
        except BaseException:
            with contextlib.suppress(Exception):
                await ws.close()
            raise
        self._ws = ws
        self._reader = asyncio.create_task(self._read())
        self._player = asyncio.create_task(self._play_loop())
        self._keepalive = asyncio.create_task(self._keep_alive())

    def _system_prompt(self) -> str:
        """The think prompt. Not ``_prompt``: that name is the engine's "say this now" method,
        which the watchdog (time limit, idle, cost cap) and the supervisor's Say use.

        Over the limit, the end of the system text goes first: its last section is KNOWLEDGE,
        while the persona, guardrails, identity rules and instructions come first and stay."""
        hint = ["(use the search_knowledge tool to look facts up before answering)"] \
            if self.brain.retrieve is not None else []
        system = self.brain._system(hint)
        tail = ("\n\nYou are speaking on a phone call. The greeting has already been said: "
                "do not greet again. Keep replies short and natural.")
        if len(system) + len(tail) > PROMPT_LIMIT:
            cut = "\n[…knowledge shortened…]"
            system = system[:PROMPT_LIMIT - len(tail) - len(cut)] + cut
        return system + tail

    def settings(self) -> dict[str, Any]:
        """The ``Settings`` message: μ-law 8 kHz both ways, Nova-3 listening, a hosted model
        with our prompt and tools, the chosen voice, and our greeting."""
        import os

        functions = [{"name": t.name, "description": t.description, "parameters": t.parameters}
                     for t in self.brain.tools]
        if self.brain.retrieve is not None:
            functions.append({k: SEARCH_TOOL[k] for k in ("name", "description", "parameters")})
        agent: dict[str, Any] = {
            "listen": {"provider": {"type": "deepgram", "model": str(self.va.get("listen_model") or "nova-3")}},
            "think": {"provider": think_provider(self.va), "prompt": self._system_prompt(), "functions": functions},
            "speak": speak_provider(self.va, dict(os.environ)),
        }
        if self.greeting:
            agent["greeting"] = self.greeting
        return {"type": "Settings",
                "audio": {"input": {"encoding": "mulaw", "sample_rate": 8000},
                          "output": {"encoding": "mulaw", "sample_rate": 8000, "container": "none"}},
                "agent": agent}

    async def stop(self) -> None:
        for task in (self._keepalive, self._greeting_guard):
            if task is not None and not task.done():
                task.cancel()
        await super().stop()

    async def _keep_alive(self) -> None:
        while self._ws is not None and not self.finished.is_set():
            await asyncio.sleep(KEEPALIVE_S)
            with contextlib.suppress(Exception):
                await self._send({"type": "KeepAlive"})

    # -- caller audio and text ----------------------------------------------------------------
    async def on_audio(self, mulaw: bytes) -> None:
        if self._ws is None:
            return await CallSession.on_audio(self, mulaw)
        if self.finished.is_set():
            return
        if self.recorder is not None:
            self.recorder.caller(mulaw)
        if getattr(self.brain, "paused", False) or self.transfer_pending:
            return                      # a person is handling the call: the agent hears nothing
        with contextlib.suppress(Exception):
            await self._ws.send(mulaw)

    async def _reply(self, text: str, record_as: str | None = None) -> None:
        """Typed text (browser tests) and keypad digits: a user message to the agent."""
        if self._ws is None:
            return await CallSession._reply(self, text, record_as)
        if len(text) < 2 and record_as is None:
            return
        self._last_activity = time.time()
        self._record("caller", record_as or text)
        if getattr(self.brain, "paused", False):
            return
        self._user_said_at = time.time()
        self._first_audio_pending = True
        await self._send({"type": "InjectUserMessage", "content": text})

    async def _respond(self) -> None:
        """The agent answers by itself (nothing to ask for)."""

    # -- agent events ---------------------------------------------------------------------------
    async def _read(self) -> None:
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    await self._audio(raw)
                    continue
                try:
                    await self._on_event(json.loads(raw))
                except Exception:  # noqa: BLE001 - one bad event must not end the call
                    _log.exception("voice agent event failed")
        except Exception as exc:  # noqa: BLE001
            if not self.finished.is_set():
                _log.warning("voice agent connection lost: %s", exc)
        if not self.finished.is_set():
            self.event("voice_agent_lost")
            await self._hangup("the Deepgram Voice Agent connection was lost")

    async def _audio(self, chunk: bytes) -> None:
        if not chunk or getattr(self.brain, "paused", False):
            return
        if self._first_audio_pending and not self._greeting_live:
            self._first_audio_pending = False
            total = (time.time() - self._user_said_at) * 1000 if self._user_said_at else 0.0
            self.latencies_ms.append(total)
            self.turn_timings.append({"total_ms": round(total, 1), "stt": "voice_agent",
                                      "llm": f"voice_agent:{self.rt_model}",
                                      "tts": f"voice_agent:{self.rt_voice}", "fallbacks": [], "error": ""})
        self._model_speaking = True
        await self._play.put(chunk)

    async def _on_event(self, ev: dict[str, Any]) -> None:
        kind = ev.get("type", "")
        if kind == "ConversationText":
            text = str(ev.get("content", "")).strip()
            if not text:
                return
            if ev.get("role") == "user":
                self._last_activity = time.time()
                self._user_said_at = time.time()
                self._first_audio_pending = True
                self._record("caller", text)
            else:
                if not self._greeting_text_seen and _same_words(text, self.greeting):
                    self._greeting_text_seen = True       # the greeting: recorded at the start
                    return
                self._greeting_live = False               # the agent has moved past the greeting
                self._record("agent", text)
                if hasattr(self.brain, "said"):
                    self.brain.said(text)
        elif kind == "UserStartedSpeaking":
            self._last_activity = time.time()
            self._idle_prompts = 0
            if self._greeting_live:
                return                  # the disclosure is never cut off: answered after it
            if self._model_speaking or self._speaking:
                await self._barge_in()
        elif kind == "AgentStartedSpeaking" and self._greeting_text_seen:
            self._greeting_live = False                   # a reply after the greeting
        elif kind == "FunctionCallRequest":
            for fn in ev.get("functions") or []:
                if fn.get("client_side", True):
                    await self._function(fn)
        elif kind == "AgentAudioDone":
            self._last_activity = time.time()
            await self._drain()
            self._greeting_live = False
            await self._after_turn()
        elif kind == "Error":
            err = str(ev.get("description") or ev.get("code") or ev)
            self.event("voice_agent_error", err[:200])
            self.turn_timings.append({"stt": "voice_agent", "llm": f"voice_agent:{self.rt_model}",
                                      "tts": f"voice_agent:{self.rt_voice}", "fallbacks": [], "error": err[:300]})
        elif kind == "Warning":
            self.event("voice_agent_warning", str(ev.get("description") or ev)[:200])

    async def _drain(self, timeout: float = 30.0) -> None:
        """Wait until what the agent said has been played (before a hang-up or transfer)."""
        end = time.time() + timeout
        while (self._model_speaking or not self._play.empty()) and time.time() < end \
                and not self.finished.is_set():
            await asyncio.sleep(0.05)

    async def _function(self, fn: dict[str, Any]) -> None:
        name = str(fn.get("name", ""))
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = {}
        if name == "search_knowledge" and self.brain.retrieve is not None:
            try:
                hits = await self.brain.retrieve(str(args.get("query", "")))
                result = "\n---\n".join(hits) if hits else "(nothing relevant found)"
            except Exception:  # noqa: BLE001
                result = "(the knowledge base is unavailable right now)"
        else:
            try:
                result = self.brain._run_tool(name, args)
                self.brain.state.events.append(f"{name}: {result}")
            except Exception as exc:  # noqa: BLE001 - answer anyway, or Deepgram waits for ever
                _log.exception("tool %s failed", name)
                result = f"(the {name} tool failed: {type(exc).__name__}; carry on without it)"
        self.event("tool", f"{name}: {result[:80]}")
        await self._send({"type": "FunctionCallResponse", "id": fn.get("id", ""), "name": name,
                          "content": result[:4000]})

    async def _barge_in(self) -> None:
        if self._ws is None:
            return await CallSession._barge_in(self)
        self.event("barge_in")
        while not self._play.empty():
            self._play.get_nowait()
        self._model_speaking = False
        if self.clear_audio:
            with contextlib.suppress(Exception):
                await self.clear_audio()
        await CallSession._barge_in(self)


async def think_models(api_key: str, *, transport: Any = None) -> list[dict[str, str]]:
    """The Deepgram-hosted models the agent can think with: the live list, else the built-in one."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=10.0, transport=transport) as client:
            resp = await client.get(MODELS_URL, headers={"Authorization": f"Token {api_key}"} if api_key else {})
            resp.raise_for_status()
            data = resp.json()
        rows = data.get("models", data) if isinstance(data, dict) else data
        models = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            provider, model = str(row.get("provider") or ""), str(row.get("id") or row.get("model") or "")
            if provider in THINK_PROVIDERS and model:
                models.append({"provider": provider, "model": model, "name": str(row.get("name") or model)})
        if models:
            return models
    except Exception as exc:  # noqa: BLE001 - the built-in list still works
        _log.info("Voice Agent model list unavailable (%s): using the built-in list", exc)
    return [dict(m) for m in FALLBACK_THINK_MODELS]


def probe_settings(va: dict[str, Any], env: dict[str, str]) -> dict[str, Any]:
    """Settings for the dashboard's test: the chosen listening, model and voice, a tiny prompt."""
    return {"type": "Settings",
            "audio": {"input": {"encoding": "mulaw", "sample_rate": 8000},
                      "output": {"encoding": "mulaw", "sample_rate": 8000, "container": "none"}},
            "agent": {"listen": {"provider": {"type": "deepgram", "model": str(va.get("listen_model") or "nova-3")}},
                      "think": {"provider": think_provider(va),
                                "prompt": "You are a friendly phone assistant being tested. Answer in one short sentence."},
                      "speak": speak_provider(va, env)}}


async def probe(va: dict[str, Any], api_key: str, env: dict[str, str], *, url: str = DEFAULT_URL,
                text: str = "Hello, can you hear me? Please say hello back in one short sentence.",
                timeout_s: float = 20.0) -> dict[str, Any]:
    """Connect, apply the settings, send one typed turn and time the agent's first audio:
    ``{ok, connect_ms, first_audio_ms, reply, audio_seconds, error}``."""
    from websockets.asyncio.client import connect

    out: dict[str, Any] = {"ok": False, "model": think_provider(va), "voice": voice_label(va)}
    if not api_key:
        out["error"] = "no Deepgram key: add DEEPGRAM_API_KEY in Settings → API keys (or the Speech card)"
        return out
    started = time.monotonic()
    try:
        async with asyncio.timeout(timeout_s):
            async with connect(url, additional_headers={"Authorization": f"Token {api_key}"},
                               max_size=None, close_timeout=1) as ws:
                audio = 0
                sent_at = 0.0
                async for raw in ws:
                    if isinstance(raw, bytes):
                        if sent_at and not audio:
                            out["first_audio_ms"] = round((time.monotonic() - sent_at) * 1000)
                        audio += len(raw)
                        continue
                    msg = json.loads(raw)
                    kind = msg.get("type")
                    if kind == "Welcome":
                        await ws.send(json.dumps(probe_settings(va, env)))
                    elif kind == "SettingsApplied":
                        out["connect_ms"] = round((time.monotonic() - started) * 1000)
                        sent_at = time.monotonic()
                        await ws.send(json.dumps({"type": "InjectUserMessage", "content": text}))
                    elif kind == "ConversationText" and msg.get("role") == "assistant":
                        out["reply"] = (str(out.get("reply") or "") + " " + str(msg.get("content") or "")).strip()
                    elif kind == "AgentAudioDone":
                        out["audio_seconds"] = round(audio / 8000, 2)
                        out["ok"] = audio > 0
                        if not audio:
                            out["error"] = "the agent answered with no audio"
                        break
                    elif kind == "Error":
                        out["error"] = str(msg.get("description") or msg.get("code") or "error")
                        break
    except TimeoutError:
        out["error"] = f"no answer within {timeout_s:.0f} s"
    except Exception as exc:  # noqa: BLE001 - shown in the card
        msg = f"{type(exc).__name__}: {exc}"
        if "401" in msg or "403" in msg:
            msg += " (check the Deepgram key)"
        out["error"] = msg[:300]
    return out
