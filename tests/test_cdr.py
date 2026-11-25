"""CDR report: every AI call, searchable, with totals and a CSV export; campaign calls get the
CRM's full card."""

from __future__ import annotations

import csv
import io
import json

import pytest

from tests.conftest import ScriptedProvider
from tests.test_voip import FakeSTT, FakeTTS


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XPLOGENT_HOME", str(tmp_path))
    from xplogent.core.config import load_config, save_user_config

    save_user_config({"voip": {"company": "Acme"}})
    return load_config()


def _seed(store):
    """Four real calls, a test call and a demo call, at known times."""
    def call(direction, frm, to, outcome, summary, start, dur, **kw):
        cid = store.start_call(direction, frm, to, test=kw.get("test", False), transport=kw.get("transport", "sip"),
                               line=kw.get("line", "trunk"))
        store.end_call(cid, outcome=outcome, summary=summary, cost=kw.get("cost", 0.01))
        store._write("UPDATE calls SET started_at=?, ended_at=? WHERE id=?", (start, start + dur, cid))
        if kw.get("brief"):
            store.set_call_brief(cid, kw["brief"])
        if kw.get("name") or kw.get("reason"):
            store.set_call_cdr(cid, caller_name=kw.get("name"), end_reason=kw.get("reason"))
        return cid

    ids = {
        "in1": call("inbound", "+447700900123", "+441130000000", "callback", "Wants care work in Leeds",
                    1_000_000, 125, name="Sarah Khan", reason="caller hung up", cost=0.05),
        "out1": call("outbound", "+441130000000", "+447700900555", "interested", "Keen on nights",
                     1_000_100, 300, brief={"campaign_id": 4, "applicant_id": 77,
                                            "sale": {"job_title": "Night Care Assistant"},
                                            "candidate": {"name": "Ali Raza"}}, reason="the agent ended the call"),
        "out2": call("outbound", "", "+447700900666", "no_answer", "Not answered (480)", 1_000_200, 0,
                     brief={"campaign_id": 4}, reason="not answered: 480", line="extension"),
        "in2": call("inbound", "+447700900999", "+441130000000", "resolved", "Asked about =SUM(A1) pay",
                    2_000_000, 60, transport="telnyx", line=""),
    }
    call("inbound", "+447700900123", "x", "resolved", "TEST", 1_500_000, 10, test=True)
    store.add_demo_call({"direction": "inbound", "from_number": "+1555", "to_number": "x", "outcome": "resolved",
                         "summary": "DEMO", "cost": 0, "started_at": 1_600_000, "ended_at": 1_600_010,
                         "transport": "sip"}, [("caller", "demo words")])
    store._write("INSERT INTO call_turns (call_id, speaker, text, created_at) VALUES (?,?,?,?)",
                 (ids["in2"], "caller", "do you sponsor visas", 2_000_001))
    return ids


def test_search_filters_and_totals(home):
    from xplogent.memory.store import Store

    with Store(home.db_path) as store:
        ids = _seed(store)
        all_ = store.search_calls()
        assert [r["id"] for r in all_["rows"]] == [ids["in2"], ids["out2"], ids["out1"], ids["in1"]]
        t = all_["totals"]
        assert t["calls"] == 4 and t["answered"] == 3 and t["inbound"] == 2 and t["outbound"] == 2
        assert t["talk_s"] == 125 + 300 + 60                      # unanswered dials add no talk time

        def ids_of(**kw):
            return [r["id"] for r in store.search_calls(**kw)["rows"]]

        assert ids_of(q="07700 900123") == [ids["in1"]]          # a number however it is typed
        assert ids_of(q="Sarah") == [ids["in1"]]                  # the name
        assert ids_of(q="night care") == [ids["out1"]]            # the job, from the brief
        assert ids_of(q="Ali Raza") == [ids["out1"]]
        assert ids_of(q="visas") == []                            # transcripts only when asked
        assert ids_of(q="visas", transcripts=True) == [ids["in2"]]
        assert ids_of(q="50%") == []                              # % is literal, not a wildcard
        assert ids_of(direction="outbound") == [ids["out2"], ids["out1"]]
        assert ids_of(answered="no") == [ids["out2"]]
        assert ids_of(outcome="callback") == [ids["in1"]]
        assert ids_of(line="extension") == [ids["out2"]]
        assert ids_of(line="telnyx") == [ids["in2"]]
        assert ids_of(line="sip") == [ids["out1"], ids["in1"]]
        assert ids_of(campaign_id=4) == [ids["out2"], ids["out1"]]
        assert ids_of(date_from=1_000_050, date_to=1_000_250) == [ids["out2"], ids["out1"]]
        assert len(ids_of(include_test=True)) == 5 and len(ids_of(include_demo=True)) == 5
        assert ids_of(sort="longest")[0] == ids["out1"]
        assert ids_of(sort="oldest")[0] == ids["in1"]

        row = next(r for r in all_["rows"] if r["id"] == ids["out1"])
        assert row["number"] == "+447700900555" and row["name"] == "Ali Raza"
        assert row["job"] == "Night Care Assistant" and row["campaign_id"] == 4 and row["applicant_id"] == 77
        assert row["duration_s"] == 300 and row["answered"] is True and row["line_label"] == "SIP trunk"
        assert row["end_reason"] == "the agent ended the call"
        page2 = store.search_calls(limit=2, offset=2)
        assert [r["id"] for r in page2["rows"]] == [ids["out1"], ids["in1"]] and page2["total"] == 4
        assert set(store.cdr_outcomes()) >= {"callback", "interested", "no_answer", "resolved"}


def test_cdr_api_and_csv(home):
    from fastapi.testclient import TestClient

    from xplogent.interfaces.api.server import create_app
    from xplogent.memory.store import Store

    with Store(home.db_path) as store:
        ids = _seed(store)
    client = TestClient(create_app())
    r = client.get("/voip/cdr", params={"q": "+447700900123", "per_page": 10}).json()
    assert r["ok"] and r["total"] == 1 and r["rows"][0]["id"] == ids["in1"] and "callback" in r["outcomes"]
    r = client.get("/voip/cdr", params={"direction": "outbound", "answered": "yes", "from": "bad"}).json()
    assert [x["id"] for x in r["rows"]] == [ids["out1"]]

    res = client.get("/voip/cdr.csv")
    assert res.status_code == 200 and "attachment" in res.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(res.text.lstrip("﻿"))))
    assert rows[0][:5] == ["Call ID", "Started", "Ended", "Direction", "Customer number"]
    assert len(rows) == 5
    summary = {r[0]: r[-1] for r in rows[1:]}
    assert summary[str(ids["in2"])] == "Asked about =SUM(A1) pay"        # mid-text: harmless
    number = {r[0]: r[4] for r in rows[1:]}
    assert number[str(ids["in1"])] == "'+447700900123"                    # never read as a formula


def test_finished_calls_record_name_and_how_they_ended(home):
    from xplogent.memory.store import Store
    from xplogent.voip.brain import CallState
    from xplogent.voip.service import end_reason

    class S:
        timeline = [{"kind": "answered"}, {"kind": "hangup", "detail": "caller silent"}]

    assert end_reason(S(), CallState()) == "caller silent"
    S.timeline = []
    assert end_reason(S(), CallState()) == "caller hung up"
    assert end_reason(S(), CallState(transfer=True)) == "transferred to a person"

    from xplogent.voip.dialler import log_unanswered_call

    call_id, outcome = log_unanswered_call(home, "+447700900777", {"candidate": {"name": "Zara"}}, "SIP 486 Busy Here")
    with Store(home.db_path) as store:
        row = store.search_calls(q="Zara")["rows"][0]
    assert row["id"] == call_id and outcome == "busy" and row["answered"] is False
    assert row["end_reason"].startswith("not answered: SIP 486")


def test_a_campaign_call_gets_the_crms_full_card(home):
    from xplogent.memory.store import Store
    from xplogent.voip.service import build_session

    brief = {"applicant_id": 5, "sale_id": 7, "campaign_id": 1, "campaign_item_id": 9,
             "sale": {"job_title": "Night Care Assistant", "salary": "£13"},
             "candidate": {"applicant_id": 5, "name": "Sarah Khan", "first_name": "Sarah"},
             "history": [{"date": "2026-11-01", "sale_id": 7, "job": "CAMPAIGN HISTORY", "stage": "crm"}]}
    lookup = {"ok": True, "found": True, "contact": {
        "applicant_id": 5, "name": "Sarah Khan", "notes": "Prefers nights",
        "history": [{"date": "2026-10-01", "sale_id": 3, "job": "LOOKUP HISTORY", "stage": "quality"}],
        "interviews": [{"sale_id": 7, "date": "2099-01-05", "time": "10:00", "status": 1}],
        "applications": [{"sale_id": 7, "job": "Night Care Assistant", "latest_stage": "crm"}],
        "calls": [{"at": "2026-11-20T10:00:00Z", "direction": "outbound", "by": "Hina", "duration_s": 120}]}}

    async def send(_b):
        return None

    with Store(home.db_path) as store:
        _s, call_id, brain = build_session(
            home, store, {"direction": "outbound", "from": "+441130000000", "to": "+447700900123",
                          "brief": brief, "crm_lookup": lookup},
            send_audio=send, stt=FakeSTT(), tts=FakeTTS(), provider=ScriptedProvider([]), retrieve=None)
        stored = json.loads(store._query("SELECT brief FROM calls WHERE id=?", (call_id,))[0][0])
        row = store.search_calls(q="Sarah Khan")["rows"][0]
    system = brain._system([])
    assert "Pay: £13" in system                                   # the campaign's own data stays
    assert "CAMPAIGN HISTORY" in system and "LOOKUP HISTORY" not in system   # never overwritten
    assert "Mon 05 Jan 2099 at 10:00: Night Care Assistant (booked)" in system
    assert "outbound by Hina" in system
    assert stored["campaign_item_id"] == 9 and stored["interviews"]
    assert row["id"] == call_id                                   # the name is in the CDR from the start

    other = dict(lookup, contact={**lookup["contact"], "applicant_id": 99})
    with Store(home.db_path) as store:
        _s, _id, brain2 = build_session(
            home, store, {"direction": "outbound", "to": "+447700900123", "brief": brief, "crm_lookup": other},
            send_audio=send, stt=FakeSTT(), tts=FakeTTS(), provider=ScriptedProvider([]), retrieve=None)
    assert "INTERVIEWS" not in brain2._system([])                 # someone else's card is never mixed in
