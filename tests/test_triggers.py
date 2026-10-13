"""Proactive triggers: persistence, webhook lookup, and file-change firing."""

from __future__ import annotations

import pytest

from tests.conftest import ScriptedProvider
from xplogent.core import triggers as trig_mod
from xplogent.core.config import load_config
from xplogent.core.triggers import FileWatcher, run_trigger
from xplogent.memory.store import Store
from xplogent.providers.base import Message, Role


def test_trigger_persistence_and_token_lookup(tmp_path):
    store = Store(tmp_path / "m.db")
    tid = store.add_trigger("hook", "webhook", "tok123", "do the thing")
    assert store.get_trigger_by_token("tok123")["id"] == tid
    assert store.get_trigger_by_token("nope") is None
    store.toggle_trigger(tid)
    assert store.get_trigger(tid)["enabled"] == 0
    store.delete_trigger(tid)
    assert store.get_trigger(tid) is None
    store.close()


@pytest.mark.asyncio
async def test_run_trigger_records_status(monkeypatch):
    monkeypatch.setattr(
        "xplogent.runtime.build_provider",
        lambda *_a, **_k: ScriptedProvider([Message(role=Role.ASSISTANT, content="handled")]),
    )
    cfg = load_config()
    store = Store(cfg.db_path)
    tid = store.add_trigger("hook", "webhook", "tok", "summarize: {{}}")
    store.close()

    out = await run_trigger({"id": tid, "prompt": "summarize", "mode": "agent"},
                            context="payload body", config=cfg)
    assert out == "handled"
    store = Store(cfg.db_path)
    assert store.get_trigger(tid)["last_status"] == "ok"
    store.close()


@pytest.mark.asyncio
async def test_file_watcher_fires_on_change(monkeypatch, tmp_path):
    fired: list[dict] = []

    async def fake_run(trigger, context="", config=None):
        fired.append({"trigger": trigger, "context": context})
        return ""

    monkeypatch.setattr(trig_mod, "run_trigger", fake_run)

    cfg = load_config()
    watched = tmp_path / "inbox.txt"
    watched.write_text("one")
    store = Store(cfg.db_path)
    store.add_trigger("watch", "file", str(watched), "summarize new entries")
    store.close()

    w = FileWatcher(cfg, tick=999)
    await w._tick_once()            # first observation — must NOT fire
    assert fired == []
    _touch_later(watched, "one\ntwo")  # change it
    await w._tick_once()            # now it should fire
    await w.drain()
    assert len(fired) == 1
    assert "changed" in fired[0]["context"]


def _touch_later(path, text):
    """Write and push the mtime forward so coarse filesystem clocks still see a change."""
    import os
    import time as _time

    st = path.stat().st_mtime if path.exists() else _time.time()
    path.write_text(text)
    os.utime(path, (st + 5, st + 5))


@pytest.mark.asyncio
async def test_file_watcher_ignores_its_own_writes(monkeypatch, tmp_path):
    """A run that writes into the watched folder must not re-trigger itself forever."""
    folder = tmp_path / "inbox"
    folder.mkdir()
    (folder / "a.txt").write_text("a")
    fired: list[str] = []

    async def fake_run(trigger, context="", config=None):
        fired.append(context)
        _touch_later(folder / "summary.md", "written by the run")
        return ""

    monkeypatch.setattr(trig_mod, "run_trigger", fake_run)
    cfg = load_config()
    store = Store(cfg.db_path)
    store.add_trigger("watch", "file", str(folder), "summarize")
    store.close()

    w = FileWatcher(cfg, tick=999, cooldown=0)
    await w._tick_once()
    _touch_later(folder / "b.txt", "b")
    await w._tick_once()
    await w.drain()
    await w._tick_once()  # only the run's own write changed: no new fire
    await w.drain()
    assert len(fired) == 1


@pytest.mark.asyncio
async def test_file_watcher_cooldown(monkeypatch, tmp_path):
    fired: list[str] = []

    async def fake_run(trigger, context="", config=None):
        fired.append(context)
        return ""

    monkeypatch.setattr(trig_mod, "run_trigger", fake_run)
    cfg = load_config()
    watched = tmp_path / "f.txt"
    watched.write_text("0")
    store = Store(cfg.db_path)
    store.add_trigger("watch", "file", str(watched), "x")
    store.close()

    w = FileWatcher(cfg, tick=999, cooldown=3600)
    await w._tick_once()
    _touch_later(watched, "1")
    await w._tick_once()
    await w.drain()
    _touch_later(watched, "2")
    await w._tick_once()
    await w.drain()
    assert len(fired) == 1
