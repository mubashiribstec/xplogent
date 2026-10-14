"""Phone agent training: recordings, documents, Q&A sheets, playbook."""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from tests.conftest import ScriptedProvider
from xplogent.core.config import load_config
from xplogent.memory.store import Store
from xplogent.providers.base import Message, Role
from xplogent.voip import training


def _stereo_wav(path: Path, left: np.ndarray, right: np.ndarray, rate: int = 16000) -> Path:
    data = np.stack([left, right], axis=1).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data.tobytes())
    return path


def _tone(seconds: float, start: float, length: float, rate: int = 16000) -> np.ndarray:
    t = np.arange(int(seconds * rate)) / rate
    sig = np.zeros_like(t)
    on = (t >= start) & (t < start + length)
    sig[on] = 8000 * np.sin(2 * np.pi * 440 * t[on])
    return sig


class _Seg:
    def __init__(self, start, end, text):
        self.start, self.end, self.text = start, end, text


class FakeWhisper:
    """'Transcribes' by reporting where the channel is loud."""

    def __init__(self):
        self.calls = 0

    def transcribe(self, audio, **kw):
        self.calls += 1
        loud = np.flatnonzero(np.abs(audio) > 0.05)
        if not len(loud):
            return iter([]), None
        start = loud[0] / 16000
        return iter([_Seg(start, start + 1.0, f"words at {start:.0f}s")]), None


def test_classify_and_safe_names():
    assert training.classify("call.MP3") == "recording"
    assert training.classify("faq.csv") == "qa"
    assert training.classify("script.pdf") == "document"
    assert training.classify("virus.exe") == ""
    assert training.safe_name("../../etc/pass wd;rm.txt") == "pass wd_rm.txt"


@pytest.mark.parametrize("text, expected", [
    ("Question,Answer\nWhat are your hours?,9 to 5\n,skip\nPrice?,$10\n",
     [("What are your hours?", "9 to 5"), ("Price?", "$10")]),
    ("id;answer;question\n1;Yes we do;Do you ship?\n", [("Do you ship?", "Yes we do")]),
    ("Do you deliver?,Yes\nOpen Sunday?,No\n", [("Do you deliver?", "Yes"), ("Open Sunday?", "No")]),
    ("﻿q\ta\nhi\thello\n", [("hi", "hello")]),
    ("", []),
])
def test_parse_qa_csv(text, expected):
    assert training.parse_qa_csv(text) == expected


def test_stereo_recording_is_split_into_agent_and_customer(tmp_path, monkeypatch):
    fake = FakeWhisper()
    monkeypatch.setattr(training, "_whisper_model", lambda name: fake)
    wav = _stereo_wav(tmp_path / "call.wav", _tone(6, 0, 1), _tone(6, 3, 1))
    seen = []
    text = training.transcribe_recording(wav, progress=seen.append)
    assert text.splitlines() == ["[0:00] Agent: words at 0s", "[0:03] Customer: words at 3s"]
    assert fake.calls == 2 and seen and max(seen) < 1
    swapped = training.transcribe_recording(wav, layout="agent-right")
    assert swapped.splitlines()[0] == "[0:00] Customer: words at 0s"
    # A "stereo" file with the same audio on both channels is one speaker.
    same = _stereo_wav(tmp_path / "mono.wav", _tone(4, 1, 1), _tone(4, 1, 1))
    assert training.transcribe_recording(same) == "[0:01] Speaker: words at 1s"


def test_merge_turns_joins_same_speaker():
    turns = training.merge_turns([(5, "Customer", "c"), (0, "Agent", "a1"), (2, "Agent", "a2")])
    assert turns == [(0, "Agent", "a1 a2"), (5, "Customer", "c")]


def _item(kind, name, content: bytes, **opts) -> int:
    path = training.training_dir() / name
    path.write_bytes(content)
    with Store(load_config().db_path) as store:
        return store.add_training_item(kind, name, str(path), len(content), opts)


def _provider(spec):
    return ScriptedProvider([])


async def test_process_qa_document_and_recording_then_delete():
    qa = _item("qa", "faq.csv", b"question,answer\nHours?,9-5\nShip abroad?,Yes, to the EU\n")
    doc = _item("document", "script.md", b"# Opening\n\nHi, this is Sam from Acme.\n")
    rec = _item("recording", "call.wav", b"RIFF", layout="agent-left")

    def fake_transcribe(path, **kw):
        kw["progress"](0.5)
        assert kw["layout"] == "agent-left"
        return "[0:00] Agent: Hello\n[0:02] Customer: How much is it?"

    for item_id in (qa, doc, rec):
        res = await training.process_item(item_id, make_provider=_provider, transcribe=fake_transcribe)
        assert res["ok"], res
    with Store(load_config().db_path) as store:
        items = {i["id"]: i for i in store.list_training_items()}
        assert all(i["status"] == "done" and i["doc_id"] for i in items.values())
        assert items[qa]["chunks"] == 2
        chunks = [c["content"] for c in store.all_chunks()]
        assert "Q: Ship abroad?\nA: Yes, to the EU" in chunks
        assert any("How much is it?" in c for c in chunks)
        assert store.get_training_item(rec)["transcript"].startswith("[0:00] Agent")
        assert training.delete_item(store, qa)
        assert "Q: Hours?\nA: 9-5" not in [c["content"] for c in store.all_chunks()]
        assert store.get_training_item(qa) is None
        assert not (training.training_dir() / "faq.csv").exists()


async def test_bad_items_report_an_error():
    bad = _item("qa", "empty.csv", b"only one column here\n")
    res = await training.process_item(bad, make_provider=_provider)
    assert not res["ok"]
    with Store(load_config().db_path) as store:
        it = store.get_training_item(bad)
    assert it["status"] == "error" and "question" in it["error"]


async def test_playbook_is_built_from_transcripts():
    rec = _item("recording", "c.wav", b"x")
    await training.process_item(rec, make_provider=_provider,
                                transcribe=lambda p, **k: "[0:00] Agent: hi\n[0:01] Customer: price?")
    prompts = []

    class Recorder(ScriptedProvider):
        async def stream(self, messages, tools=None, **kw):
            prompts.append(messages[-1].content)
            async for ev in super().stream(messages, tools, **kw):
                yield ev

    provider = Recorder([Message(role=Role.ASSISTANT, content="- customer asked the price"),
                         Message(role=Role.ASSISTANT, content="# Playbook\n## Call flow\n- greet")])
    job = training.PlaybookJob()
    draft = await training.build_playbook(load_config(), provider, job=job)
    assert draft.startswith("# Playbook") and job.progress == 1.0
    assert "Customer: price?" in prompts[0] and "customer asked the price" in prompts[1]


async def test_playbook_needs_material():
    with pytest.raises(ValueError, match="upload"):
        await training.build_playbook(load_config(), ScriptedProvider([]))


# ── routes ───────────────────────────────────────────────────────────────────
@pytest.fixture
def client(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from xplogent.interfaces.api.routes import voip as voip_routes
    from xplogent.interfaces.api.server import create_app

    kicked = []
    monkeypatch.setattr(voip_routes, "_kick_training", lambda: kicked.append(1))
    c = TestClient(create_app())
    c.kicked = kicked
    return c


def test_upload_list_view_delete(client):
    files = [("files", ("faq.csv", b"question,answer\nHi?,Hello\n", "text/csv")),
             ("files", ("call.mp3", b"ID3fake", "audio/mpeg")),
             ("files", ("tool.exe", b"MZ", "application/octet-stream")),
             ("files", ("id_rsa.txt", b"x", "text/plain"))]
    res = client.post("/voip/training/upload", files=files, data={"layout": "agent-right"}).json()
    assert [a["kind"] for a in res["added"]] == ["qa", "recording"]
    assert {r["file"] for r in res["rejected"]} == {"tool.exe", "id_rsa.txt"}
    assert client.kicked
    items = client.get("/voip/training").json()["items"]
    rec = next(i for i in items if i["kind"] == "recording")
    assert rec["options"] == {"layout": "agent-right"} and rec["status"] == "queued"
    assert Path(rec["path"]).parent == training.training_dir()
    assert client.get(f"/voip/training/{rec['id']}").json()["item"]["filename"] == "call.mp3"
    assert client.post(f"/voip/training/{rec['id']}/retry", json={"layout": "agent-left"}).json()["ok"]
    assert client.get(f"/voip/training/{rec['id']}").json()["item"]["options"]["layout"] == "agent-left"
    assert client.delete(f"/voip/training/{rec['id']}").json()["ok"]
    assert not Path(rec["path"]).exists()
    bad = client.post("/voip/training/upload", files=[("files", ("a.exe", b"x", "x/y"))])
    assert bad.status_code == 400


def test_playbook_save_and_build(client, monkeypatch):
    from xplogent.interfaces.api import deps
    from xplogent.voip.service import load_playbook

    assert client.put("/voip/playbook", json={"content": "# Playbook\n- be kind"}).json()["ok"]
    assert load_playbook() == "# Playbook\n- be kind\n"
    assert client.get("/voip/playbook").json()["content"].startswith("# Playbook")

    async def fake_build(cfg, provider, job=None):
        return "# Playbook\n## Call flow"

    monkeypatch.setattr(training, "build_playbook", fake_build)
    monkeypatch.setattr(deps, "build_provider", lambda spec, **kw: ScriptedProvider([]))
    training.playbook_job.reset()
    with client:
        assert client.post("/voip/playbook/build").json()["job"]["status"] == "running"
        for _ in range(50):
            job = client.get("/voip/playbook").json()["job"]
            if job["status"] != "running":
                break
    assert job["status"] == "done" and job["draft"] == "# Playbook\n## Call flow"
