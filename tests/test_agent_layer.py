"""Agent-layer basics (vs-Retell brief Part C2/C3): the greeting is spoken once
with a system-injected AI disclosure the operator cannot edit away."""

from __future__ import annotations

import asyncio

import pytest

from tests.conftest import ScriptedProvider
from tests.test_voip import FakeTTS, make_session, tone_mulaw
from xplogent.voip.service import (
    AI_DISCLOSURE,
    call_variables,
    effective_greeting,
    render_template,
)


def test_disclosure_injected_when_greeting_lacks_it():
    g = effective_greeting({"greeting": "Hello, thanks for calling {company}. How can I help?",
                            "company": "Acme"})
    assert g.startswith(AI_DISCLOSURE)
    assert "calling Acme." in g


def test_disclosure_not_duplicated_when_greeting_has_it():
    text = "Hi, you're speaking with an AI assistant at Acme. How can I help?"
    assert effective_greeting({"greeting": text}) == text


def test_disclosure_toggle_off_keeps_operator_greeting():
    assert effective_greeting({"greeting": "Hello there.", "disclose_ai": False}) == "Hello there."


def test_disclosure_with_empty_greeting_still_discloses():
    assert effective_greeting({}) == AI_DISCLOSURE


def test_brain_guardrail_forbids_repeating_the_greeting():
    from xplogent.voip.brain import GUARDRAILS

    assert "already been spoken" in GUARDRAILS
    assert "do not greet again" in GUARDRAILS.lower()


# ── C5: silence recovery — at most 2 short prompts, then a polite end ──────────

async def _run_watchdog(session, seconds=2.5):
    task = asyncio.create_task(session._watch())
    await asyncio.sleep(seconds)
    session.finished.set()
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


@pytest.mark.asyncio
async def test_silence_gets_two_prompts_then_polite_hangup():
    session, sent, cleared, turns, hung = make_session(
        ScriptedProvider([]), tts=FakeTTS(chunks=2),
        idle_prompt_s=0.5, idle_hangup_s=3.0, idle_max_prompts=2)
    # The watchdog ticks every 1s: prompt 1 at the first tick, prompt 2 once
    # idle passes the midpoint, then the polite goodbye + hangup.
    await _run_watchdog(session, 9.0)
    agent_lines = [t for s, t in turns if s == "agent"]
    assert agent_lines[0] == "Are you still there?"
    assert agent_lines[1] == "I still can't hear you — are you there?"
    assert any("end the call" in t for t in agent_lines)
    assert len([t for t in agent_lines if "still there" in t or "hear you" in t]) == 2
    assert hung  # the call was ended politely, not left hanging
    await session.stop()


@pytest.mark.asyncio
async def test_speaking_resets_the_silence_prompt_count():
    session, *_ = make_session(
        ScriptedProvider([]), tts=FakeTTS(chunks=2),
        idle_prompt_s=0.2, idle_hangup_s=60, idle_max_prompts=2)
    session._idle_prompts = 2
    await session.on_audio(tone_mulaw(200))
    assert session._idle_prompts == 0
    await session.stop()


# ── C9: dynamic variables in greeting / persona / playbook ─────────────────────



def test_render_template_both_syntaxes_and_unknown_left_alone():
    out = render_template("Hi {{caller_name}}, welcome to {company}. Ref: {missing}",
                          {"caller_name": "Ana", "company": "Acme"})
    assert out == "Hi Ana, welcome to Acme. Ref: {missing}"


def test_call_variables_include_contact_fields():
    contact = {"name": "Ana", "fields": {"plan": "Gold", "company": "Other"}}
    variables = call_variables({"company": "Acme"}, {"direction": "incoming"}, contact, "+44123")
    assert variables["caller_name"] == "Ana"
    assert variables["caller_phone"] == "+44123"
    assert variables["direction"] == "inbound"
    assert variables["plan"] == "Gold"
    assert variables["company"] == "Acme"  # config wins over a contact field


# ── C10: unanswered questions are logged for the knowledge-gap report ──────────


@pytest.mark.asyncio
async def test_zero_hit_retrieval_is_logged_as_a_knowledge_gap(tmp_path, monkeypatch):
    import json

    monkeypatch.setenv("XPLOGENT_HOME", str(tmp_path))
    from xplogent.core.config import load_config
    from xplogent.memory.store import Store
    from xplogent.voip.service import _Retriever, knowledge_gap_path

    class _NoVectors:
        async def embed_one(self, _text):
            return []

    cfg = load_config()
    store = Store(cfg.db_path)
    retriever = _Retriever(cfg, store, 3, embedder=_NoVectors())
    assert await retriever("do you offer weekend appointments?") == []
    lines = knowledge_gap_path().read_text(encoding="utf-8").strip().splitlines()
    assert json.loads(lines[-1])["query"] == "do you offer weekend appointments?"
    store.close()


def test_top_knowledge_gaps_counts_and_orders(tmp_path, monkeypatch):
    monkeypatch.setenv("XPLOGENT_HOME", str(tmp_path))
    from xplogent.voip.service import log_knowledge_gap, top_knowledge_gaps

    log_knowledge_gap("do you open on sundays?")
    log_knowledge_gap("what does it cost?")
    log_knowledge_gap("do you open on sundays?")
    gaps = top_knowledge_gaps()
    assert gaps[0] == {"query": "do you open on sundays?", "count": 2}
    assert gaps[1] == {"query": "what does it cost?", "count": 1}
