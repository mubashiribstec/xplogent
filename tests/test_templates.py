"""Part E: recruitment templates as data + the pilot-plan scoring rubric."""

from __future__ import annotations

from xplogent.voip.templates import TEMPLATES, get_template, score_screen

FULL = {"right_to_work": True, "must_haves": "all", "commute": "yes",
        "shifts": "full", "experience": "relevant"}


def test_templates_registered_and_unknown_is_none():
    assert set(TEMPLATES) == {"kingsbury_inbound", "kingsbury_outbound_screener"}
    assert get_template("nope") is None and get_template("") is None


def test_inbound_template_carries_the_field_schema():
    fields = {f["name"]: f for f in TEMPLATES["kingsbury_inbound"]["fields"]}
    phone = fields["callback_number"]
    assert phone["validator"] == "phone" and phone["require_confirmed"] is True
    assert fields["caller_name"]["required"] is True


def test_no_template_or_rubric_output_uses_the_banned_outcome_word():
    banned = "reject"
    for template in TEMPLATES.values():
        assert banned not in template["playbook"].lower()
        assert banned not in template["greeting"].lower()
    for answers in (FULL, {"right_to_work": False}, {}):
        out = score_screen(answers)
        assert banned not in out["band"].lower()
        assert banned not in out["summary"].lower()


def test_scoring_full_marks_is_qualified_at_100():
    out = score_screen(FULL)
    assert out == {"band": "Qualified", "score": 100, "gate_failed": False,
                   "flags": [], "summary": "Score 100/100 → Qualified. "
                                           "A recruiter makes the final decision."}


def test_scoring_right_to_work_gate_fails_closed_without_a_score():
    out = score_screen({**FULL, "right_to_work": False})
    assert out["gate_failed"] is True and out["score"] is None
    assert out["band"] == "Flagged — human review"


def test_scoring_bands_and_partial_credit():
    out = score_screen({"right_to_work": True, "must_haves": "partial",
                        "must_haves_missing": ["DBS"], "commute": "partial",
                        "shifts": "partial", "experience": "some"})
    assert out["score"] == 20 + 10 + 10 + 10  # 50 → Flagged
    assert out["band"] == "Flagged"
    assert any("DBS" in f for f in out["flags"])
    low = score_screen({"right_to_work": True, "must_haves": "none",
                        "commute": "no", "shifts": "no", "experience": "none"})
    assert low["score"] == 0 and low["band"] == "Flagged — likely not suitable"


def test_pay_mismatch_flags_but_never_reduces_the_score():
    out = score_screen({**FULL, "pay_discussion_needed": True})
    assert out["score"] == 100 and out["band"] == "Qualified"
    assert out["flags"] == ["pay_discussion_needed"]


def test_build_session_with_template_applies_playbook_fields_and_guard(tmp_path, monkeypatch):
    monkeypatch.setenv("XPLOGENT_HOME", str(tmp_path))
    from tests.conftest import ScriptedProvider
    from tests.test_voip import FakeSTT, FakeTTS
    from xplogent.core.config import load_config, save_user_config
    from xplogent.memory.store import Store
    from xplogent.voip.service import build_session

    save_user_config({"voip": {"enabled": True, "template": "kingsbury_inbound",
                               "company": "Kingsbury Personnel"}})
    cfg = load_config()
    store = Store(cfg.db_path)
    sent = []

    async def send(frame):
        sent.append(frame)

    session, call_id, brain = build_session(
        cfg, store, {"direction": "incoming", "from": "+447700900999", "to": "+442071234567"},
        send_audio=send, stt=FakeSTT(), tts=FakeTTS(), provider=ScriptedProvider([]),
        retrieve=None)
    assert "care, nursing, legal and hospitality" in brain.playbook
    assert session.greeting.startswith("Hello, you've reached Kingsbury Personnel")
    # The Part D guard is live with the template's schema:
    assert brain._run_tool("end_call", {}).startswith("REFUSED")
    brain._run_tool("record_field", {"name": "caller_name", "value": "Paul Vincent"})
    brain._run_tool("record_field", {"name": "callback_number", "value": "07700900123"})
    assert brain._run_tool("end_call", {}).startswith("REFUSED")  # not yet read back
    brain._run_tool("record_field", {"name": "callback_number",
                                     "value": "07700900123", "confirmed": True})
    assert brain._run_tool("end_call", {}) == "call will end after your goodbye"
    store.close()


def test_template_only_applies_to_calls_in_its_direction(tmp_path, monkeypatch):
    monkeypatch.setenv("XPLOGENT_HOME", str(tmp_path))
    from tests.conftest import ScriptedProvider
    from tests.test_voip import FakeSTT, FakeTTS
    from xplogent.core.config import load_config, save_user_config
    from xplogent.memory.store import Store
    from xplogent.voip.service import build_session

    save_user_config({"voip": {"enabled": True, "template": "kingsbury_outbound_screener",
                               "company": "Kingsbury Personnel",
                               "greeting": "Thanks for calling {company}."}})
    cfg = load_config()
    store = Store(cfg.db_path)

    async def send(frame):
        pass

    def session_for(direction):
        return build_session(cfg, store, {"direction": direction, "from": "+447700900999",
                                          "to": "+442071234567"},
                             send_audio=send, stt=FakeSTT(), tts=FakeTTS(),
                             provider=ScriptedProvider([]), retrieve=None)

    inbound, _, inbound_brain = session_for("incoming")
    assert "candidate_name" not in inbound.greeting and "Thanks for calling Kingsbury" in inbound.greeting
    assert "SCREENING QUESTIONS" not in inbound_brain.playbook
    outbound, _, outbound_brain = session_for("outgoing")
    assert outbound.greeting.startswith("Hello, is that") and "SCREENING QUESTIONS" in outbound_brain.playbook
    store.close()
