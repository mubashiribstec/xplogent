"""Call recording: stereo WAV on the call clock, routes, retention, readiness."""

from __future__ import annotations

import os
import time
import wave
from array import array

import pytest

from tests.conftest import ScriptedProvider
from tests.test_voip import FakeSTT, FakeTTS
from xplogent.core.config import load_config, save_user_config
from xplogent.memory.store import Store
from xplogent.voip import recording
from xplogent.voip.audio import pcm16_to_mulaw
from xplogent.voip.readiness import checklist


def _tone(n: int, amp: int = 8000) -> bytes:
    return pcm16_to_mulaw(array("h", [amp if i % 2 else -amp for i in range(n)]).tobytes())


def _channels(path):
    with wave.open(str(path)) as w:
        assert (w.getnchannels(), w.getframerate(), w.getsampwidth()) == (2, 8000, 2)
        data = array("h")
        data.frombytes(w.readframes(w.getnframes()))
    return data[0::2], data[1::2]


def test_both_sides_land_on_the_call_clock(tmp_path):
    now = [0.0]
    rec = recording.CallRecorder(tmp_path / "c.wav", clock=lambda: now[0])
    for _ in range(25):                       # caller talks for the first 0.5 s
        rec.caller(_tone(160))
    now[0] = 1.0
    rec.agent(_tone(160))                     # agent's first frame goes out at 1.0 s
    now[0] = 3.0
    rec.caller(_tone(160))                    # caller stream stalled: realigned to 3.0 s
    left, right = _channels(rec.close())
    assert abs(left[100]) > 1000 and abs(left[6000]) < 50          # speech, then silence
    assert abs(right[100]) < 50 and abs(right[8000 + 10]) > 1000    # agent starts at 1 s
    assert abs(left[3 * 8000 + 10]) > 1000
    assert rec.close() is None                                     # closing twice is harmless


def test_empty_call_writes_nothing(tmp_path):
    assert recording.CallRecorder(tmp_path / "x.wav").close() is None


def test_prune_respects_retention(tmp_path):
    old = recording.recordings_dir() / "1.wav"
    new = recording.recordings_dir() / "2.wav"
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    os.utime(old, (time.time() - 100 * 86400,) * 2)
    assert recording.prune(90) == 1 and not old.exists() and new.exists()
    assert recording.prune(0) == 0


@pytest.fixture
def recorded_call():
    from xplogent.voip.service import build_session, finalize_call

    cfg = load_config()
    store = Store(cfg.db_path)

    async def send(_b):
        return None

    session, call_id, brain = build_session(cfg, store, {"direction": "incoming", "from": "+1555"},
                                            send_audio=send, stt=FakeSTT(), tts=FakeTTS(),
                                            provider=ScriptedProvider([]), retrieve=None)
    assert session.recorder is not None
    session.recorder.caller(_tone(1600))
    finalize_call(cfg, store, call_id, session, brain)
    yield store, call_id
    store.close()


def test_recording_routes(recorded_call):
    from fastapi.testclient import TestClient

    from xplogent.interfaces.api.routes import voip as voip_routes
    from xplogent.interfaces.api.server import create_app

    store, call_id = recorded_call
    path = store.get_call(call_id)["recording"]
    assert path.endswith(f"{call_id}.wav") and os.path.exists(path)
    kicked = []
    voip_routes_kick = voip_routes._kick_training
    voip_routes._kick_training = lambda: kicked.append(1)
    try:
        c = TestClient(create_app())
        res = c.get(f"/voip/calls/{call_id}/recording")
        assert res.status_code == 200 and res.headers["content-type"] == "audio/wav" and res.content[:4] == b"RIFF"
        assert c.post(f"/voip/calls/{call_id}/recording/train").json()["ok"] and kicked
        item = store.list_training_items()[0]
        assert item["kind"] == "recording" and item["options"] == {"layout": "agent-right"}
        assert c.delete(f"/voip/calls/{call_id}/recording").json()["ok"] and not os.path.exists(path)
        assert c.get(f"/voip/calls/{call_id}/recording").status_code == 404
        store.set_call_recording(call_id, "/etc/passwd")          # never served from elsewhere
        assert c.get(f"/voip/calls/{call_id}/recording").status_code == 404
    finally:
        voip_routes._kick_training = voip_routes_kick


def test_recording_can_be_switched_off():
    from xplogent.voip.service import build_session

    save_user_config({"voip": {"recording": {"enabled": False}}})
    store = Store(load_config().db_path)

    async def send(_b):
        return None

    session, *_ = build_session(load_config(), store, {"direction": "incoming", "from": "+1"}, send_audio=send,
                                stt=FakeSTT(), tts=FakeTTS(), provider=ScriptedProvider([]), retrieve=None)
    assert session.recorder is None
    store.close()
    # The shipped greeting says "transcribed": true with recording off, nothing to flag.
    items = {i["id"]: i for i in checklist(load_config().voip, "ollama:x", env={})}
    assert "greeting" not in items
    # A greeting that claims recording while it is off is flagged.
    save_user_config({"voip": {"greeting": "This call is recorded. How can I help?"}})
    items = {i["id"]: i for i in checklist(load_config().voip, "ollama:x", env={})}
    assert not items["greeting"]["ok"] and "recorded" in items["greeting"]["detail"]
