"""Warm-up before an outbound call (handover 8 Nov, Part 3 #12): the opening line is
synthesised while the phone rings, staged on disk, used once, and swept after a day."""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from tests.conftest import ScriptedProvider
from tests.test_voip import FakeTTS, make_session
from xplogent.core.config import load_config, save_user_config
from xplogent.providers.base import Message, Role
from xplogent.voip import warmup

BRIEF = {"applicant_id": 123, "sale_id": 45, "source": "crm_agent_call", "lock_token": "secret-lease",
         "sale": {"job_title": "Care Assistant", "postcode_district": "LS6"}, "candidate": {"first_name": "Sarah"}}


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    monkeypatch.setenv("XPLOGENT_HOME", str(tmp_path))
    warmup._CACHE.clear()
    save_user_config({"voip": {"company": "Kingsbury", "recording": {"enabled": True}}})
    yield
    warmup._CACHE.clear()


def _greeting():
    cfg = load_config()
    return cfg, warmup._opening(cfg, "+447700900123", {"brief": BRIEF, "transport": "sip", "line": "extension"})[1]


async def test_prepare_synthesises_the_exact_opening_line_and_stages_it():
    cfg, greeting = _greeting()
    assert "Am I speaking with Sarah?" in greeting and "AI assistant" in greeting
    tts = FakeTTS()
    key = await warmup.prepare(cfg, "+447700900123", {"brief": BRIEF, "transport": "sip"}, tts=tts)
    assert key and tts.spoken                                   # each part of the greeting synthesised
    staged = warmup.staging_dir() / f"{key}.json"
    assert staged.exists() and "secret-lease" not in staged.read_text()   # the lease never on disk
    audio = warmup.take(cfg.voip, greeting)
    assert set(audio) == set(tts.spoken) and all(chunks for chunks, _ in audio.values())
    assert not staged.exists()                                  # used once, then removed
    assert warmup.take(cfg.voip, greeting) == {}


async def test_other_voice_settings_never_get_the_prepared_audio():
    cfg, greeting = _greeting()
    await warmup.prepare(cfg, "+447700900123", {"brief": BRIEF}, tts=FakeTTS())
    other = {**cfg.voip, "tts": {"provider": "cartesia", "voice": "x"}}
    assert warmup.take(other, greeting) == {}


async def test_the_session_plays_the_prepared_audio_without_synthesising():
    greeting = "Hi, this is Kingsbury's AI assistant, on a recorded line. Am I speaking with Sarah?"
    from xplogent.voip.engine import split_greeting_disclosure
    parts = [p for p in split_greeting_disclosure(greeting) if p]  # how the session speaks it
    assert len(parts) == 2
    prepared = {p: ([b"\x10\x20" * 2400] * 3, 24000) for p in parts}
    prov = ScriptedProvider([Message(role=Role.ASSISTANT, content="Great.")])
    session, sent, _c, turns, _h = make_session(prov, tts=FakeTTS(), greeting=greeting, prepared_audio=prepared)
    await session.start()
    for _ in range(100):
        if session._greeting_task and session._greeting_task.done():
            break
        await asyncio.sleep(0.02)
    assert sent and session.tts.spoken == []                    # nothing synthesised during the call
    assert any(e["kind"] == "prepared_audio" for e in session.timeline)
    await session.stop()


def test_staged_data_older_than_a_day_is_swept():
    folder = warmup.staging_dir()
    folder.mkdir(parents=True, exist_ok=True)
    old, new = folder / "old.json", folder / "new.json"
    old.write_text("{}")
    new.write_text("{}")
    day_ago = time.time() - 25 * 3600
    os.utime(old, (day_ago, day_ago))
    assert warmup.sweep() == 1
    assert not old.exists() and new.exists()


async def test_dial_starts_the_warm_up_for_a_crm_call(monkeypatch):
    from xplogent.voip import service as service_mod

    save_user_config({"voip": {"enabled": True, "telephony": "sip", "calling_hours": {"enabled": False}}})
    started = []
    monkeypatch.setattr(warmup, "start", lambda cfg, number, meta: started.append((number, meta.get("brief"))))
    svc = service_mod.VoipService()

    async def dialer(number, meta):
        return {"ok": True, "via": "sip", "call": {"sip_call_id": "x@y"}}
    svc.sip_dialer = dialer
    assert (await svc.dial("+447700900123", via="sip", brief=dict(BRIEF)))["ok"]
    assert started and started[0][1]["applicant_id"] == 123
    started.clear()
    await svc.dial("+447700900124", via="sip")                  # no brief: nothing to prepare
    assert not started
