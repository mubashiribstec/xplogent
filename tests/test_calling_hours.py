"""WP3: calling hours per weekday, UK bank holidays, the clocks changing."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from xplogent.voip import hours
from xplogent.voip.config_check import check_voip
from xplogent.voip.control import calling_hours_problem

UK = {"calling_hours": {"enabled": True, "start": "09:00", "end": "20:00", "timezone": "Europe/London",
                        "days": {"mon": "", "tue": "", "wed": "", "thu": "", "fri": "",
                                 "sat": "10:00-16:00", "sun": "closed"},
                        "bank_holidays": "closed", "bank_holiday_region": "england-and-wales"}}


def at(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


def test_weekday_and_saturday_windows():
    assert calling_hours_problem(UK, None, at(2026, 11, 4, 12)) == ""           # Wed noon
    late = calling_hours_problem(UK, None, at(2026, 11, 4, 20, 30))
    assert late.startswith("outside calling hours") and "Wed 09:00–20:00" in late
    assert calling_hours_problem(UK, None, at(2026, 11, 7, 11)) == ""           # Sat 11:00
    sat = calling_hours_problem(UK, None, at(2026, 11, 7, 17))
    assert "Sat 10:00–16:00" in sat and "17:00" in sat


def test_sunday_and_bank_holidays_are_closed():
    assert "no calls on Sundays" in calling_hours_problem(UK, None, at(2026, 11, 8, 12))
    xmas = calling_hours_problem(UK, None, at(2026, 12, 25, 12))
    assert "UK bank holiday (Christmas Day)" in xmas
    assert "Boxing Day" in calling_hours_problem(UK, None, at(2026, 12, 28, 12))   # substitute day
    open_rule = {"calling_hours": {**UK["calling_hours"], "bank_holidays": "open"}}
    assert calling_hours_problem(open_rule, None, at(2026, 12, 25, 12)) == ""     # a Friday
    # Scotland has St Andrew's Day (30 Nov 2026, a Monday); England doesn't.
    scot = {"calling_hours": {**UK["calling_hours"], "bank_holiday_region": "scotland"}}
    assert "St Andrew" in calling_hours_problem(scot, None, at(2026, 11, 30, 12))
    assert calling_hours_problem(UK, None, at(2026, 11, 30, 12)) == ""


def test_the_clocks_changing():
    # Summer time: 08:30 UTC is 09:30 in London (open); in winter it's 08:30 (closed).
    assert calling_hours_problem(UK, None, at(2026, 6, 1, 8, 30)) == ""
    assert "08:30" in calling_hours_problem(UK, None, at(2026, 11, 2, 8, 30))
    # The clocks go back on Sun 25 Oct 2026 and forward on Sun 28 Mar 2027.
    assert calling_hours_problem(UK, None, at(2026, 10, 26, 8, 30)) != ""        # Mon, now GMT
    assert calling_hours_problem(UK, None, at(2027, 3, 30, 8, 30)) == ""         # Tue, now BST
    assert "Easter Monday" in calling_hours_problem(UK, None, at(2027, 3, 29, 12))


def test_old_settings_keep_working():
    plain = {"calling_hours": {"start": "09:00", "end": "20:00", "timezone": "UTC"}}
    assert calling_hours_problem(plain, None, at(2026, 11, 8, 12)) == ""          # Sunday is open
    assert calling_hours_problem(plain, None, at(2026, 12, 25, 12)) == ""         # holidays open
    never = {"calling_hours": {"start": "00:00", "end": "00:00", "timezone": "UTC"}}
    assert calling_hours_problem(never, None, at(2026, 11, 4, 12)) != ""
    overnight = {"calling_hours": {"start": "22:00", "end": "06:00", "timezone": "UTC"}}
    assert calling_hours_problem(overnight, None, at(2026, 11, 4, 3)) == ""
    assert calling_hours_problem(overnight, None, at(2026, 11, 4, 12)) != ""


def test_next_allowed_skips_weekends_and_christmas():
    nxt = hours.next_allowed(UK, None, at(2026, 11, 7, 17))                         # Sat 17:00
    assert nxt.isoformat() == "2026-11-09T09:00:00+00:00"                           # Mon 09:00
    nxt = hours.next_allowed(UK, None, at(2026, 12, 24, 21))                        # Christmas Eve
    assert nxt.isoformat() == "2026-12-26T10:00:00+00:00"                           # Sat 26th
    assert hours.next_allowed(UK, None, at(2026, 11, 4, 12, 0)) == at(2026, 11, 4, 12, 0)
    shut = {"calling_hours": {**UK["calling_hours"], "start": "00:00", "end": "00:00",
                              "days": {d: "closed" for d in hours.DAYS}}}
    assert hours.next_allowed(shut, None, at(2026, 11, 4, 12)) is None


def test_contact_time_zone_wins():
    # 12:00 UTC on a Wednesday is 04:00 in Los Angeles.
    msg = calling_hours_problem(UK, {"timezone": "America/Los_Angeles"}, at(2026, 11, 4, 12))
    assert "America/Los_Angeles" in msg and "04:00" in msg


def test_bad_day_settings_are_reported_and_fail_closed():
    bad = {"calling_hours": {**UK["calling_hours"], "days": {**UK["calling_hours"]["days"], "mon": "9 to 5"}}}
    assert any("days.mon" in p for p in check_voip(bad))
    assert calling_hours_problem(bad, None, at(2026, 11, 2, 12)) != ""             # Monday: closed
    assert check_voip(UK) == []


async def test_bank_holidays_refresh_cache_and_fallback(monkeypatch):
    monkeypatch.setenv("XPLOGENT_BANK_HOLIDAYS_URL", "https://www.gov.uk/bank-holidays.json")
    body = {"england-and-wales": {"division": "england-and-wales", "events": [
        {"title": "Test Day", "date": "2029-01-01"}, {"title": "A", "date": "2029-04-01"},
        {"title": "B", "date": "2029-05-01"}, {"title": "C", "date": "2029-06-01"},
        {"title": "D", "date": "2029-07-01"}]}}
    ok = httpx.MockTransport(lambda r: httpx.Response(200, json=body))
    res = await hours.refresh_bank_holidays(transport=ok)
    assert res["status"] == "updated"
    days = hours.bank_holidays("england-and-wales")
    assert days["2029-01-01"] == "Test Day" and days["2026-12-25"] == "Christmas Day"  # merged
    assert (await hours.refresh_bank_holidays(transport=ok))["status"] == "fresh"      # weekly
    broken = httpx.MockTransport(lambda r: httpx.Response(500))
    res = await hours.refresh_bank_holidays(force=True, transport=broken)
    assert res["status"] == "failed" and hours.bank_holidays()["2029-01-01"] == "Test Day"
    hours.cache_path().write_text("not json", encoding="utf-8")
    assert hours.bank_holidays()["2026-12-25"] == "Christmas Day"                      # bundled
    monkeypatch.setenv("XPLOGENT_BANK_HOLIDAYS_URL", "")
    assert (await hours.refresh_bank_holidays(force=True))["status"] == "offline"


def test_parse_gov_uk_ignores_junk():
    assert hours.parse_gov_uk({"scotland": {"events": [{"date": "x", "title": "y"}, None]}}) == {}
    assert hours.parse_gov_uk([]) == {}


@pytest.fixture
def client():
    from xplogent.interfaces.api.server import create_app

    with TestClient(create_app()) as c:
        yield c


def test_hours_endpoints(client):
    r = client.get("/voip/hours").json()
    assert r["timezone"] == "Europe/London" and len(r["week"]) == 7
    assert {d["day"]: d["window"] for d in r["week"]}["sun"] == "closed"
    assert r["bank_holidays"] == "closed" and r["upcoming_bank_holidays"]
    bad = client.post("/voip/hours", json={"days": {"sun": "all day"}}).json()
    assert bad["ok"] is False and "days.sun" in bad["error"]
    assert client.post("/voip/hours", json={"timezone": "Mars/Base"}).json()["ok"] is False
    r = client.post("/voip/hours", json={"days": {"sun": "11:00-15:00"}, "bank_holidays": "open"}).json()
    assert r["ok"] and {d["day"]: d["window"] for d in r["week"]}["sun"] == "11:00–15:00"
    assert r["bank_holidays"] == "open"
    assert {d["day"]: d["window"] for d in client.get("/voip/hours").json()["week"]}["sun"] == "11:00–15:00"
