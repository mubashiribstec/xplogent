
"""Scheduler: NL parsing, due detection, firing a (scripted) run, one-shot."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta

import pytest

from xplogent.core.scheduler import Scheduler, next_run_after, parse_schedule
from xplogent.memory.store import Store


def test_parse_every_n_hours():
    spec, nxt = parse_schedule("every 2 hours")
    assert spec == "every 2 hours"
    assert nxt - time.time() == pytest.approx(2 * 3600, abs=5)


def test_parse_daily_at_time_is_in_future():
    spec, nxt = parse_schedule("every day at 9am")
    assert "9am" in spec
    assert nxt > time.time()
    # the next run should be at 09:00 local
    assert datetime.fromtimestamp(nxt).hour == 9


def test_parse_weekly():
    spec, nxt = parse_schedule("every monday at 08:00")
    assert datetime.fromtimestamp(nxt).weekday() == 0
    assert datetime.fromtimestamp(nxt).hour == 8


def test_parse_minutes():
    _spec, nxt = parse_schedule("every 15 minutes")
    assert nxt - time.time() == pytest.approx(15 * 60, abs=5)


def test_one_shot_does_not_repeat():
    spec, nxt = parse_schedule("in 10 minutes")
    assert nxt > time.time()
    # after it fires, there is no further run
    assert next_run_after(spec, "", after=nxt) is None


def test_unrecognized_raises():
    with pytest.raises(ValueError):
        parse_schedule("whenever I feel like it")


def test_advancing_recomputes_next():
    spec, nxt = parse_schedule("every day at 9am")
    after = next_run_after(spec, "", after=nxt)
    assert after is not None
    assert after - nxt == pytest.approx(86400, abs=5)


class _Cfg:
    def __init__(self, db_path):
        self.db_path = db_path
        self.scheduler = {"enabled": True, "tick_seconds": 30}


@pytest.mark.asyncio
async def test_scheduler_fires_due_job(tmp_path, monkeypatch):
    db = tmp_path / "s.db"
    store = Store(db)
    # A job already due (next_run in the past), repeating daily.
    past = (datetime.now() - timedelta(minutes=1)).timestamp()
    sid = store.add_schedule("morning", "say hi", "agent", "every day at 9am", "", past)
    store.close()

    ran = {"prompts": []}
    sched = Scheduler(_Cfg(db))

    async def fake_run_job(job):
        ran["prompts"].append(job["prompt"])

    monkeypatch.setattr(sched, "_run_job", fake_run_job)

    store = Store(db)
    try:
        await sched._tick_once()
        await sched.drain()
    finally:
        store.close()

    assert ran["prompts"] == ["say hi"]
    # next_run advanced into the future
    store = Store(db)
    job = store.get_schedule(sid)
    store.close()
    assert job["next_run"] > time.time()
    assert job["last_status"] == "ok"


@pytest.mark.asyncio
async def test_disabled_job_does_not_fire(tmp_path, monkeypatch):
    db = tmp_path / "s.db"
    store = Store(db)
    past = (datetime.now() - timedelta(minutes=1)).timestamp()
    sid = store.add_schedule("x", "nope", "agent", "every day at 9am", "", past)
    store.set_schedule_enabled(sid, False)
    store.close()

    sched = Scheduler(_Cfg(db))
    ran = {"n": 0}

    async def fake_run_job(job):
        ran["n"] += 1

    monkeypatch.setattr(sched, "_run_job", fake_run_job)
    await sched._tick_once()
    await sched.drain()
    assert ran["n"] == 0


@pytest.mark.asyncio
async def test_due_jobs_run_concurrently(tmp_path, monkeypatch):
    """A slow job must not hold up the other due jobs."""
    db = tmp_path / "s.db"
    store = Store(db)
    past = (datetime.now() - timedelta(minutes=1)).timestamp()
    store.add_schedule("slow", "slow", "agent", "every day at 9am", "", past)
    store.add_schedule("fast", "fast", "agent", "every day at 9am", "", past)
    store.close()

    sched = Scheduler(_Cfg(db))
    release = asyncio.Event()
    done: list[str] = []

    async def fake_run_job(job):
        if job["prompt"] == "slow":
            await release.wait()
        done.append(job["prompt"])

    monkeypatch.setattr(sched, "_run_job", fake_run_job)
    await sched._tick_once()
    for _ in range(50):
        if "fast" in done:
            break
        await asyncio.sleep(0.01)
    assert done == ["fast"]
    # A second tick while "slow" is still running must not start it again.
    await sched._tick_once()
    release.set()
    await sched.drain()
    assert sorted(done) == ["fast", "slow"]


@pytest.mark.asyncio
async def test_stop_cancels_hung_job(tmp_path, monkeypatch):
    db = tmp_path / "s.db"
    store = Store(db)
    past = (datetime.now() - timedelta(minutes=1)).timestamp()
    sid = store.add_schedule("hang", "hang", "agent", "every day at 9am", "", past)
    store.close()

    sched = Scheduler(_Cfg(db), tick=0.01, stop_grace=0.05)

    async def fake_run_job(job):
        await asyncio.Event().wait()

    monkeypatch.setattr(sched, "_run_job", fake_run_job)
    monkeypatch.setattr(sched, "_realign", lambda: None)
    await sched.start()
    await asyncio.sleep(0.05)
    await asyncio.wait_for(sched.stop(), timeout=2)
    store = Store(db)
    assert store.get_schedule(sid)["last_status"] == "cancelled"
    store.close()


def test_missed_one_shot_fires_after_restart(tmp_path):
    db = tmp_path / "s.db"
    store = Store(db)
    past = time.time() - 600
    sid = store.add_schedule("once", "remind", "agent", "in 5 minutes", "", past)
    store.close()

    Scheduler(_Cfg(db))._realign()
    store = Store(db)
    job = store.get_schedule(sid)
    store.close()
    # Due now, not pushed another 5 minutes out.
    assert job["next_run"] <= time.time()
