"""Demo mode: sample calls for showing the product, flagged ``demo=1``.

They go straight into the call table (no telephony, no CRM, no contacts, no
training) and are badged "Demo data" in the dashboard. Counts and the go-live
checklist leave them out, and one click removes them without touching real calls.
"""

from __future__ import annotations

import time
from typing import Any

_SAMPLES: list[dict[str, Any]] = [
    {"call": {"direction": "inbound", "from_number": "+447700900101", "to_number": "+442071234567",
              "outcome": "callback", "transport": "sip", "line": "trunk", "cost": 0.031,
              "summary": "New client: needs 3 care assistants in Leeds from Monday. Callback booked.",
              "minutes_ago": 95, "seconds": 162},
     "turns": [("agent", "Hello, thanks for calling Kingsbury Personnel. You're speaking with an AI assistant, "
                         "and this call is recorded so we can help you better. How can I help you?"),
               ("caller", "Hi, I need three care assistants for a home in Leeds, starting Monday."),
               ("agent", "I can help with that. May I take your name and the best number for a recruiter "
                         "to call you back?"),
               ("caller", "It's Priya Shah, 07700 900101."),
               ("agent", "Thank you, Priya. That's 0 7 7 0 0, 9 0 0, 1 0 1. Is that right?"),
               ("caller", "Yes."),
               ("agent", "A senior recruiter will call you back within two working hours. Thank you for calling.")]},
    {"call": {"direction": "inbound", "from_number": "+447700900102", "to_number": "2001",
              "outcome": "info", "transport": "sip", "line": "extension", "cost": 0.012, "via_queue": 1,
              "original_called_number": "+442071234567", "queue_wait_s": 48,
              "summary": "Came through the office queue after 48 s. Asked about weekend opening hours.",
              "minutes_ago": 60, "seconds": 64},
     "turns": [("agent", "Hello, thanks for waiting. You're speaking with an AI assistant, and this call is "
                         "recorded. How can I help?"),
               ("caller", "Are you open on Saturdays?"),
               ("agent", "Yes, the office is open on Saturdays from nine until one. Anything else I can help with?"),
               ("caller", "No, that's all, thanks.")]},
    {"call": {"direction": "outbound", "from_number": "+442071234567", "to_number": "+447700900103",
              "outcome": "transferred", "transport": "telnyx", "cost": 0.044,
              "summary": "Screening call for the night nurse role: right to work confirmed, asked for a recruiter.",
              "minutes_ago": 30, "seconds": 210},
     "turns": [("agent", "Hello, is that Daniel? I'm an AI assistant calling from Kingsbury Personnel about the "
                         "night nurse role you applied for. This call is recorded. Is now a good time?"),
               ("caller", "Yes, go ahead."),
               ("agent", "First, can I confirm you have the right to work in the UK?"),
               ("caller", "Yes, I'm a British citizen. Can I talk to a recruiter about pay?"),
               ("agent", "Of course, I'll put you through to a recruiter now.")]},
    {"call": {"direction": "inbound", "from_number": "+447700900104", "to_number": "+442071234567",
              "outcome": "do_not_call", "transport": "sip", "line": "trunk", "cost": 0.006,
              "summary": "Asked not to be called again: added to the do-not-call list.",
              "minutes_ago": 12, "seconds": 28},
     "turns": [("agent", "Hello, thanks for calling. You're speaking with an AI assistant, and this call is recorded."),
               ("caller", "Please take my number off your list."),
               ("agent", "Of course. I've removed your number, and you won't be called again. Goodbye.")]},
]


def _debug(seconds: int, n_turns: int) -> dict[str, Any]:
    turns = [{"stt_ms": 180.0 + 20 * i, "llm_first_sentence_ms": 420.0 + 35 * i, "tts_first_audio_ms": 160.0,
              "total_ms": 760.0 + 55 * i, "stt": "deepgram-stream:nova-3", "llm": "openai:gpt-4o-mini",
              "tts": "elevenlabs:demo", "fallbacks": [], "error": ""} for i in range(max(1, n_turns // 2))]
    timeline = [{"t": 0.0, "kind": "answered", "detail": ""}, {"t": 0.0, "kind": "greeting", "detail": ""},
                {"t": 5.2, "kind": "greeting_end", "detail": ""},
                {"t": float(seconds), "kind": "ended", "detail": "demo"}]
    return {"timeline": timeline, "turns": turns, "providers": {"stt_fallbacks": 0, "tts_fallbacks": 0}}


def seed(store: Any) -> int:
    """Add the sample calls (replacing earlier demo data). Returns how many."""
    store.clear_demo_calls()
    now = time.time()
    for sample in _SAMPLES:
        c = dict(sample["call"])
        started = now - c.pop("minutes_ago") * 60
        c.update(started_at=started, ended_at=started + c.pop("seconds"))
        store.add_demo_call(c, sample["turns"], _debug(int(c["ended_at"] - started), len(sample["turns"])))
    return len(_SAMPLES)
