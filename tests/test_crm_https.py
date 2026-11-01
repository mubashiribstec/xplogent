"""Audit P2: CRM credentials and call data never cross the internet in cleartext or follow a
redirect. Plain http stays fine on the local network (CRM and Xplogent on the same LAN)."""

from __future__ import annotations

import httpx
import pytest

from xplogent.core.config import load_config, save_user_config
from xplogent.voip import crm


@pytest.mark.parametrize("url,ok", [
    ("https://crm.example.com", True), ("http://crm.example.com", False), ("http://8.8.8.8", False),
    ("http://127.0.0.1:8089", True), ("http://localhost:8089", True),
    # the local network: private and link-local addresses, machine names, LAN-only suffixes
    ("http://10.0.0.5", True), ("http://192.168.1.20:8000", True), ("http://172.20.1.4", True),
    ("http://169.254.3.3", True), ("http://[fd12::5]:80", True), ("http://[::ffff:192.168.1.9]", True),
    ("http://crm-server", True), ("http://crm.local", True), ("http://crm.office.lan", True),
    ("http://172.32.0.1", False), ("ftp://crm", False), ("crm.example.com", False)])
def test_base_url_rule(url, ok):
    assert (crm.base_url_problem(url) == "") is ok


def test_allow_http_covers_an_internal_name_that_looks_public():
    assert crm.base_url_problem("http://crm.mycompany.co.uk") != ""
    assert crm.base_url_problem("http://crm.mycompany.co.uk", allow_http=True) == ""


async def test_plain_http_on_the_local_network_is_sent(monkeypatch):
    monkeypatch.setenv("TEST_CRM_TOKEN", "s3cret")
    save_user_config({"voip": {"crm": {"enabled": True, "base_url": "http://192.168.1.20",
                                       "api_token_env": "TEST_CRM_TOKEN"}}})
    seen = []
    transport = httpx.MockTransport(lambda r: seen.append(str(r.url)) or httpx.Response(200, json={"sales": []}))
    assert (await crm.post(load_config(), "sales/open", {}, transport=transport))["ok"]
    assert seen == ["http://192.168.1.20/api/xplogent/sales/open"]


async def test_no_request_and_no_token_over_http(monkeypatch):
    monkeypatch.setenv("TEST_CRM_TOKEN", "s3cret")
    save_user_config({"voip": {"crm": {"enabled": True, "base_url": "http://crm.example.com",
                                       "api_token_env": "TEST_CRM_TOKEN"}}})
    seen = []
    transport = httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200, json={}))
    res = await crm.post(load_config(), "sales/open", {}, transport=transport)
    assert not res["ok"] and res["error"].startswith("insecure address") and seen == []
    assert "https" in crm.explain_failure(res)["fix"]
    out = await crm.deliver(load_config(), {"call_id": 5}, transport=transport, attempts=1)
    assert out["status"] == "skipped" and "insecure" in out["detail"] and seen == []


async def test_redirects_are_not_followed(monkeypatch):
    monkeypatch.setenv("TEST_CRM_TOKEN", "s3cret")
    save_user_config({"voip": {"crm": {"enabled": True, "base_url": "https://crm.example.com",
                                       "api_token_env": "TEST_CRM_TOKEN"}}})
    seen = []

    def handler(r):
        seen.append(str(r.url))
        return httpx.Response(302, headers={"Location": "http://evil.example/steal"})
    res = await crm.post(load_config(), "sales/open", {}, transport=httpx.MockTransport(handler))
    assert not res["ok"] and res["status"] == 302 and len(seen) == 1
    assert "redirect" in crm.explain_failure(res)["reason"]
    out = await crm.deliver(load_config(), {"call_id": 6}, transport=httpx.MockTransport(handler),
                            attempts=1, backoff_s=0)
    assert out["status"] == "failed" and all("evil" not in u for u in seen)


def test_readiness_flags_an_insecure_crm():
    from xplogent.voip.readiness import checklist

    items = checklist({"crm": {"enabled": True, "base_url": "http://crm.example.com"}}, "openai:x", env={})
    item = next(i for i in items if i["id"] == "crm_https")
    assert not item["ok"] and "https" in item["detail"]
    lan = checklist({"crm": {"enabled": True, "base_url": "http://192.168.1.20"}}, "openai:x", env={})
    item = next(i for i in lan if i["id"] == "crm_https")
    assert item["ok"] and "local network" in item["detail"]
