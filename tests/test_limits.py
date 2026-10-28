"""WP8: call length, off-topic and not-interested limits, and cost caps."""

from __future__ import annotations

import asyncio
import time

from tests.conftest import ScriptedProvider
from tests.test_voip import FakeSTT, FakeTTS, make_session
from xplogent.core.config import load_config, save_user_config
from xplogent.memory.store import Store
from xplogent.providers.base import Message, Role, ToolCall
from xplogent.voip import alerts
from xplogent.voip.brain import CallBrain


async def wait_for(cond, timeout=5.0):
    for _ in range(int(timeout / 0.05)):
        if cond():
            return True
        await asyncio.sleep(0.05)
    return False


async def test_wrap_up_a_minute_before_the_limit():
    session, sent, cleared, turns, hung = make_session(ScriptedProvider([]), max_call_s=3.0, wrap_up_s=1.2)
    await session.start()
    assert await wait_for(lambda: any("About one minute left" in n for n in session.brain.notes), 4)
    assert any(e["kind"] == "wrap_up" for e in session.timeline) and not hung
    await session.stop()


def redirecting(n):
    return [Message(role=Role.ASSISTANT, content="Let's get back to the role.", tool_calls=[
        ToolCall(id=str(i), name="redirect", arguments={"topic": "football"})]) for i in range(n)]


async def test_third_redirect_in_a_row_closes_the_call():
    replies = []
    for _ in range(3):
        replies += [redirecting(1)[0], Message(role=Role.ASSISTANT, content="So, about the shifts?")]
    b = CallBrain(ScriptedProvider(replies))
    for _ in range(3):
        [s async for s in b.respond("Did you see the match last night?")]
    st = b.state
    assert st.end and st.exit_path == "off_topic" and st.outcome == "screen_incomplete"
    assert "third time" in st.events[-1] or any("third time" in e for e in st.events)


async def test_an_on_topic_turn_resets_the_count():
    replies = [redirecting(1)[0], Message(role=Role.ASSISTANT, content="Anyway."),
               redirecting(1)[0], Message(role=Role.ASSISTANT, content="Anyway."),
               Message(role=Role.ASSISTANT, content="Great, nights are fine then."),
               redirecting(1)[0], Message(role=Role.ASSISTANT, content="Anyway.")]
    b = CallBrain(ScriptedProvider(replies))
    for text in ("match?", "match?", "Yes I can do nights", "match?"):
        [s async for s in b.respond(text)]
    assert not b.state.end and b.state.redirects == 1


async def test_quick_close_after_not_interested():
    replies = [Message(role=Role.ASSISTANT, content="No problem at all.", tool_calls=[
        ToolCall(id="1", name="log_outcome", arguments={"outcome": "not_interested"})]),
        Message(role=Role.ASSISTANT, content="Is there anything else?")]
    session, sent, cleared, turns, hung = make_session(ScriptedProvider(replies), quick_close_s=0.5)
    await session.start()
    await session.on_text("Not interested, sorry")
    assert await wait_for(lambda: bool(hung), 5)
    assert ("agent", "Thanks for your time. Goodbye.") in turns
    assert any(e["kind"] == "quick_close" for e in session.timeline)
    await session.stop()


async def test_cost_cap_ends_the_call_politely():
    session, sent, cleared, turns, hung = make_session(
        ScriptedProvider([]), cost_rates={"per_minute": 600.0}, cost_cap=1.0)      # $10 a second
    await session.start()
    assert await wait_for(lambda: bool(hung), 5)
    assert session.brain.state.outcome == "screen_incomplete"
    assert any(e["kind"] == "cost_cap" for e in session.timeline)
    assert any("A recruiter will follow up" in t for s, t in turns if s == "agent")
    await session.stop()


def test_cost_estimate_counts_voice_and_tokens():
    session, *_ = make_session(ScriptedProvider([]), cost_rates={
        "per_minute": 0.0, "tts_per_1k_chars": 0.04, "llm_input_per_m": 1.0, "llm_output_per_m": 5.0})
    session.tts_chars = 2000
    session.brain.tokens = {"input": 100_000, "output": 10_000}
    assert session.cost_estimate() == round(0.08 + 0.1 + 0.05, 4)


async def test_token_usage_is_counted():
    b = CallBrain(ScriptedProvider([Message(role=Role.ASSISTANT, content="Hi.",
                                            usage={"input_tokens": 1200, "output_tokens": 30})]))
    [s async for s in b.respond("hello")]
    assert b.tokens == {"input": 1200, "output": 30}


def _build(info, **cfg):
    from xplogent.voip.service import build_session

    if cfg:
        save_user_config({"voip": cfg})
    c = load_config()
    store = Store(c.db_path)

    async def send(_b):
        return None
    session, call_id, brain = build_session(c, store, info, send_audio=send, stt=FakeSTT(), tts=FakeTTS(),
                                            provider=ScriptedProvider([]), retrieve=None)
    return c, store, session, call_id, brain


OUT = {"direction": "outgoing", "from": "+44113", "to": "+447700900123", "transport": "sip"}


def test_limits_come_from_the_template_and_the_campaign():
    _, store, session, _, _ = _build(OUT, template="kingsbury_outbound_screener", per_call_cost_cap=0.5,
                                     costs={"tts_per_1k_chars": 0.04})
    assert session.max_call_s == 480 and session.cost_cap == 0.5
    assert session.cost_rates["tts_per_1k_chars"] == 0.04 and session.cost_rates["per_minute"] == 0.012
    store.close()
    _, store, session, _, _ = _build({**OUT, "brief": {"max_call_seconds": 300, "per_call_cost_cap": 0.2}})
    assert session.max_call_s == 300 and session.cost_cap == 0.2
    store.close()


async def test_daily_cap_stops_new_calls_and_warns_at_80_percent():
    from xplogent.voip.service import VoipService

    alerts.reset()
    save_user_config({"voip": {"enabled": True, "daily_cost_cap": 1.0, "calling_hours": {"enabled": False}}})
    cfg = load_config()
    with Store(cfg.db_path) as s:
        cid = s.start_call("outbound", "2001", "201")
        s.end_call(cid, outcome="interested", cost=0.85)
        t = s.start_call("outbound", "2001", "202", test=True)          # tests don't count
        s.end_call(t, outcome="other", cost=5.0)
    svc = VoipService()
    dialled = []

    async def dialer(number, meta):
        dialled.append(number)
        return {"ok": True}
    svc.sip_extension_dialer = dialer
    assert (await svc.dial("201", cfg, via="extension"))["ok"]
    assert alerts.recent()[0]["kind"] == "daily_cost_80"
    with Store(cfg.db_path) as s:
        cid = s.start_call("outbound", "2001", "203")
        s.end_call(cid, outcome="interested", cost=0.2)
    res = await svc.dial("201", cfg, via="extension")
    assert not res["ok"] and res["cost_cap"] and "spending cap" in res["error"]
    assert (await svc.dial("201", cfg, via="extension", test=True))["ok"]   # test calls still work
    assert dialled == ["201", "201"]


def test_finalize_records_the_estimate():
    from xplogent.voip.service import finalize_call

    cfg, store, session, call_id, brain = _build(OUT, costs={"tts_per_1k_chars": 0.05})
    session.tts_chars = 1000
    session.started = time.time() - 60
    finalize_call(cfg, store, call_id, session, brain, "+447700900123")
    assert abs(store.get_call(call_id)["cost"] - (0.012 + 0.05)) < 0.002
    store.close()
