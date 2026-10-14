"""Settings validation, speech retry/fallback, readiness details."""

from __future__ import annotations

import pytest

from xplogent.voip.config_check import check_voip
from xplogent.voip.providers import ResilientSTT, ResilientTTS
from xplogent.voip.readiness import checklist


def test_settings_problems_are_reported():
    bad = check_voip({"enabled": "yes", "sip": {"servr": "x", "port": "5060", "ring_timeout": 30},
                      "Max_Call_Seconds": 60, "vad": {"threshold": 400}, "stt_fallback": {"provider": "x"}})
    assert "voip.enabled should be true/false, not text" in bad
    assert "voip.sip.servr is not a setting" in bad and "voip.sip.port should be a number, not text" in bad
    assert any("Max_Call_Seconds" in b and "max_call_seconds" in b for b in bad)   # did-you-mean
    assert len(bad) == 4
    assert check_voip({"enabled": True, "sip": {"allow_from": "10.0.0.1, 10.0.0.2"}}) == []
    items = {i["id"]: i for i in checklist({"enabled": "yes"}, "ollama:x", env={})}
    assert not items["settings"]["ok"] and "true/false" in items["settings"]["detail"]


class _STT:
    def __init__(self, fails, text="hi"):
        self.fails, self.text, self.calls = fails, text, 0

    async def transcribe(self, pcm):
        self.calls += 1
        if self.calls <= self.fails:
            raise RuntimeError("speech service down")
        return self.text

    async def aclose(self):
        pass


async def test_stt_retries_then_falls_back():
    flaky = _STT(fails=1)
    stt = ResilientSTT(flaky, retry_delay=0)
    assert await stt.transcribe(b"x") == "hi" and flaky.calls == 2
    down, backup = _STT(fails=99), _STT(fails=0, text="from backup")
    stt = ResilientSTT(down, backup, retry_delay=0)
    assert await stt.transcribe(b"x") == "from backup" and stt.used_fallback == 1
    with pytest.raises(RuntimeError):
        await ResilientSTT(_STT(fails=99), retry_delay=0).transcribe(b"x")


class _TTS:
    def __init__(self, rate=24000, fail_before=0, fail_after_first=False):
        self.sample_rate, self.fail_before, self.fail_after_first, self.calls = rate, fail_before, fail_after_first, 0

    async def synthesize(self, text):
        self.calls += 1
        if self.calls <= self.fail_before:
            raise RuntimeError("tts down")
        yield b"\x01\x00" * 480
        if self.fail_after_first:
            raise RuntimeError("cut off")
        yield b"\x01\x00" * 480

    async def aclose(self):
        pass


async def _collect(tts, text="hello"):
    return [c async for c in tts.synthesize(text)]


async def test_tts_retry_fallback_and_no_replay_after_audio():
    flaky = _TTS(fail_before=1)
    assert len(await _collect(ResilientTTS(flaky, retry_delay=0))) == 2 and flaky.calls == 2
    backup = _TTS(rate=8000)
    tts = ResilientTTS(_TTS(fail_before=99), backup, retry_delay=0)
    chunks = await _collect(tts)
    assert tts.used_fallback == 1 and tts.sample_rate == 24000
    assert sum(len(c) for c in chunks) > 2 * 960 * 2          # 8 kHz audio resampled up to 24 kHz
    cut = _TTS(fail_after_first=True)
    with pytest.raises(RuntimeError):
        await _collect(ResilientTTS(cut, _TTS(), retry_delay=0))
    assert cut.calls == 1                                    # not replayed once audio started


def test_readiness_flags_missing_whisper(monkeypatch):
    import importlib.util

    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name, *a: None if name == "faster_whisper" else real(name, *a))
    items = {i["id"]: i for i in checklist({"stt": {"provider": "whisper-local"},
                                            "tts": {"base_url": "http://kokoro:8880/v1"}}, "ollama:x", env={})}
    assert not items["speech"]["ok"] and "faster-whisper" in items["speech"]["detail"]
