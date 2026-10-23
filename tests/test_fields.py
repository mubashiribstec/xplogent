"""Part D mandatory-field enforcement: validators, the collector, and the
end-call guard state machine. The regression being prevented: an agent closing
a callback call 'successfully' without ever capturing the phone number."""

from __future__ import annotations

from tests.conftest import ScriptedProvider
from xplogent.voip.brain import CallBrain
from xplogent.voip.fields import build_collector

SCHEMA = [
    {"name": "callback_number", "label": "best callback number", "validator": "phone",
     "required": True, "require_confirmed": True, "default_country": "+44"},
    {"name": "caller_name", "label": "caller's name", "validator": "nonempty",
     "required": True},
]


def _brain():
    return CallBrain(ScriptedProvider([]), fields=build_collector(SCHEMA))


# ── validators ─────────────────────────────────────────────────────────────────

def test_phone_normalises_international_and_uk_national():
    c = build_collector(SCHEMA)
    ok, msg = c.set("callback_number", "+44 7700 900123")
    assert ok and c.values["callback_number"] == "+447700900123"
    c2 = build_collector(SCHEMA)
    ok, _ = c2.set("callback_number", "07700 900123")  # national + default_country
    assert ok and c2.values["callback_number"] == "+447700900123"
    c3 = build_collector(SCHEMA)
    ok, _ = c3.set("callback_number", "0044 7700 900123")
    assert ok and c3.values["callback_number"] == "+447700900123"


def test_phone_rejects_implausible_numbers():
    c = build_collector(SCHEMA)
    for bad in ("123", "call me maybe", "", "+44"):
        ok, _ = c.set("callback_number", bad)
        assert not ok, bad
    assert "callback_number" not in c.values


def test_unknown_field_and_bad_email_are_refused():
    c = build_collector([{"name": "email", "validator": "email"}])
    ok, _ = c.set("nickname", "x")
    assert not ok
    ok, _ = c.set("email", "not-an-email")
    assert not ok
    ok, _ = c.set("email", "ana@example.com")
    assert ok and c.values["email"] == "ana@example.com"


# ── collector: confirmation state ──────────────────────────────────────────────

def test_unconfirmed_phone_still_counts_as_missing():
    c = build_collector(SCHEMA)
    c.set("callback_number", "07700900123")
    c.set("caller_name", "Ana")
    assert [s.name for s in c.missing()] == ["callback_number"]
    report = c.report()
    assert report["complete"] is False and report["missing_required"] == ["callback_number"]
    ok, _ = c.set("callback_number", "07700900123", confirmed=True)
    assert ok and c.missing() == [] and c.report()["complete"] is True


def test_checklist_shows_what_is_still_open():
    brain = _brain()
    system = brain._system([])
    assert "FIELDS TO COLLECT" in system and "callback_number" in system
    brain._run_tool("record_field", {"name": "caller_name", "value": "Ana"})
    system = brain._system([])
    assert "caller_name (caller's name): Ana [confirmed]" in system


# ── the guard: end_call state machine ──────────────────────────────────────────

def test_end_call_refused_while_required_fields_open():
    brain = _brain()
    result = brain._run_tool("end_call", {})
    assert result.startswith("REFUSED")
    assert brain.state.end is False


def test_end_call_refused_until_phone_read_back_confirmed():
    brain = _brain()
    brain._run_tool("record_field", {"name": "caller_name", "value": "Ana"})
    brain._run_tool("record_field", {"name": "callback_number", "value": "07700900123"})
    assert brain._run_tool("end_call", {}).startswith("REFUSED")
    assert brain.state.end is False
    brain._run_tool("record_field", {"name": "callback_number",
                                     "value": "07700900123", "confirmed": True})
    assert brain._run_tool("end_call", {}) == "call will end after your goodbye"
    assert brain.state.end is True


def test_exit_paths_close_without_fields_and_are_recorded():
    for exit_path in ("refused", "hung_up", "wrong_party"):
        brain = _brain()
        result = brain._run_tool("end_call", {"exit": exit_path})
        assert brain.state.end is True and brain.state.exit_path == exit_path, result


def test_do_not_call_outcome_is_a_legitimate_close():
    brain = _brain()
    brain._run_tool("log_outcome", {"outcome": "do_not_call"})
    assert brain._run_tool("end_call", {}) == "call will end after your goodbye"
    assert brain.state.exit_path == "do_not_call"


def test_no_schema_means_no_guard():
    brain = CallBrain(ScriptedProvider([]))
    assert brain._run_tool("end_call", {}) == "call will end after your goodbye"
    assert brain.state.end is True


def test_record_field_without_schema_is_a_clean_noop_message():
    brain = CallBrain(ScriptedProvider([]))
    assert "no field schema" in brain._run_tool(
        "record_field", {"name": "x", "value": "y"})
