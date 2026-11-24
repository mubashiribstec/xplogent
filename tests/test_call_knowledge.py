"""Calls use the training material and the knowledge base in every mode.

- the playbook distilled from training calls reaches the prompt, also when a call template's
  script is the playbook (it used to replace it);
- the whole template script fits (the screener's ~5,000 characters were cut at 4,000);
- Voice Agent and realtime calls start with the knowledge for what the call is about, and are
  told to search before answering a question of fact; realtime looks up each caller turn.
"""

from __future__ import annotations

import asyncio

from tests.conftest import ScriptedProvider
from tests.test_realtime import FakeRealtime, heard, until
from tests.test_realtime import make as make_rt
from tests.test_voice_agent import FakeAgent
from tests.test_voice_agent import make as make_va
from tests.test_voip import FakeSTT, FakeTTS
from xplogent.voip.brain import SEARCH_RULE, CallBrain, knowledge_section, preload_knowledge
from xplogent.voip.service import call_context_query
from xplogent.voip.templates import TEMPLATES


def _build(tmp_path, monkeypatch, voip: dict, info: dict, *, playbook: str = ""):
    monkeypatch.setenv("XPLOGENT_HOME", str(tmp_path))
    from xplogent.core.config import data_dir, load_config, save_user_config
    from xplogent.memory.store import Store
    from xplogent.voip.service import build_session

    save_user_config({"voip": voip})
    cfg = load_config()
    if playbook:
        (data_dir() / "voip").mkdir(parents=True, exist_ok=True)
        (data_dir() / "voip" / "playbook.md").write_text(playbook, encoding="utf-8")

    async def send(_b):
        return None

    async def retrieve(q, **_kw):
        return []

    with Store(cfg.db_path) as store:
        session, *_ = build_session(cfg, store, info, send_audio=send, stt=FakeSTT(), tts=FakeTTS(),
                                    provider=ScriptedProvider([]), retrieve=retrieve)
    return session.brain


def test_a_template_call_keeps_the_trained_playbook_and_the_whole_script(tmp_path, monkeypatch):
    brain = _build(tmp_path, monkeypatch, {"template": "kingsbury_outbound_screener", "company": "Kingsbury"},
                   {"direction": "outgoing", "to": "+447700900123"},
                   playbook="## Objections\n- 'Is this a scam?' -> TRAINED-ANSWER-42")
    system = brain._system([])
    assert "SCRIPT / PLAYBOOK" in system and "LEARNED FROM YOUR TRAINING CALLS" in system
    assert "TRAINED-ANSWER-42" in system
    script_end = TEMPLATES["kingsbury_outbound_screener"]["playbook"].strip()[-60:]
    assert script_end in system                                  # nothing of the script is cut
    assert system.index("SCRIPT / PLAYBOOK") < system.index("LEARNED FROM YOUR TRAINING CALLS")


def test_without_a_template_the_trained_playbook_is_the_script(tmp_path, monkeypatch):
    brain = _build(tmp_path, monkeypatch, {"company": "Acme"}, {"direction": "incoming", "from": "+1"},
                   playbook="TRAINED-ONLY")
    system = brain._system([])
    assert "SCRIPT / PLAYBOOK:\nTRAINED-ONLY" in system and "LEARNED FROM" not in system


def test_the_context_query_is_what_the_call_is_about():
    brief = {"sale": {"job_title": "Night Care Assistant", "client_name": "Oak House", "salary": "£12"}}
    q = call_context_query({"company": "Kingsbury"}, brief, {"fields": {"company": "kingsbury"}})
    assert q.lower() == "night care assistant oak house kingsbury"  # deduplicated, no pay figure
    assert call_context_query({}, None, None) == ""


async def test_preload_asks_for_more_and_logs_no_gap_and_never_waits_long():
    calls = []

    async def retrieve(q, *, k=None, log_gap=True):
        calls.append((q, k, log_gap))
        return ["[source: FAQ]\nWe pay weekly."]

    brain = CallBrain(ScriptedProvider([]), retrieve=retrieve)
    brain.context_query = "Care Assistant"
    assert await preload_knowledge(brain) == ["[source: FAQ]\nWe pay weekly."]
    assert calls == [("Care Assistant", 6, False)]

    async def plain(q):                                          # a retriever without the options
        return ["plain hit"]

    brain = CallBrain(ScriptedProvider([]), retrieve=plain)
    brain.context_query = "x"
    assert await preload_knowledge(brain) == ["plain hit"]

    async def slow(q, **_kw):
        await asyncio.sleep(5)
        return ["late"]

    brain = CallBrain(ScriptedProvider([]), retrieve=slow)
    brain.context_query = "x"
    assert await preload_knowledge(brain, timeout=0.05) == [] and brain.preloaded == []
    brain.context_query = ""
    assert await preload_knowledge(brain) == []                  # nothing to ask: no lookup

    brain = CallBrain(ScriptedProvider([]))                      # no knowledge base at all
    assert knowledge_section(brain) == []


async def test_the_voice_agent_prompt_carries_the_calls_knowledge_and_the_search_rule():
    async def retrieve(q, **_kw):
        return [f"[source: Vacancy pack]\nPay for {q} is in the pack."]

    brain = CallBrain(ScriptedProvider([]), persona="You work for Acme.", retrieve=retrieve,
                      training="TRAINED-TONE")
    brain.context_query = "Night Care Assistant"
    async with FakeAgent() as fake:
        session, *_ = make_va(fake.url, brain=brain)
        await session.start()
        await until(lambda: fake.ws is not None and fake.sent("Settings"))
        prompt = fake.sent("Settings")[0]["agent"]["think"]["prompt"]
        assert SEARCH_RULE in prompt
        assert "Pay for Night Care Assistant is in the pack." in prompt
        assert "TRAINED-TONE" in prompt
        assert "search_knowledge" in {f["name"] for f in fake.sent("Settings")[0]["agent"]["think"]["functions"]}
        await session.stop()


async def test_realtime_looks_up_each_caller_turn_before_replying():
    async with FakeRealtime([]) as rt:
        session, *_ = make_rt(rt.url)
        await session.start()
        await until(lambda: rt.sent("session.update"))
        await until(lambda: session._greeting_task.done())
        await rt.push(heard("Do you deliver to Leeds?"))
        await until(lambda: rt.sent("response.create"))
        instructions = [e["session"]["instructions"] for e in rt.sent("session.update")
                        if "instructions" in e["session"]][-1]
        assert "FACT about Do you deliver to Leeds?" in instructions and SEARCH_RULE in instructions
        await session.stop()


async def test_a_slow_lookup_does_not_hold_up_the_realtime_reply(monkeypatch):
    from xplogent.voip import realtime

    monkeypatch.setattr(realtime, "LOOKUP_TIMEOUT_S", 0.05)

    async def slow(q, **_kw):
        await asyncio.sleep(5)
        return ["late"]

    brain = CallBrain(ScriptedProvider([]), persona="You work for Acme.", retrieve=slow)
    async with FakeRealtime([]) as rt:
        session, *_ = make_rt(rt.url, brain=brain)
        await session.start()
        await until(lambda: session._greeting_task.done())
        await rt.push(heard("Hello?"))
        await until(lambda: rt.sent("response.create"), t=2.0)
        assert session._turn_knowledge == []
        await session.stop()
