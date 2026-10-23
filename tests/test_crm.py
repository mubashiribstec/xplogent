"""CRM post-call delivery (Part 3a / C12): payload, HMAC, retries, fire-and-forget."""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest

from xplogent.core.config import load_config
from xplogent.voip import crm


def _cfg(tmp_path, monkeypatch, **crm_cfg):
    monkeypatch.setenv("XPLOGENT_HOME", str(tmp_path))
    from xplogent.core.config import save_user_config

    save_user_config({"voip": {"crm": {"enabled": True, "base_url": "https://crm.example.com",
                                       "api_token_env": "TEST_CRM_TOKEN", **crm_cfg}}})
    monkeypatch.setenv("TEST_CRM_TOKEN", "s3cret")
    return load_config()


def test_sign_is_hmac_sha256():
    body = b'{"a":1}'
    expected = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert crm.sign(body, "s3cret") == expected


@pytest.mark.asyncio
async def test_delivery_posts_signed_payload(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["sig"] = request.headers.get("x-xplogent-signature")
        seen["body"] = request.content
        return httpx.Response(200, json={"ok": True})

    out = await crm.deliver(cfg, {"event": "call.completed", "call_id": 7},
                            transport=httpx.MockTransport(handler), backoff_s=0)
    assert out["status"] == "sent" and out["attempts"] == 1
    assert seen["url"] == "https://crm.example.com/api/xplogent/call-summary"
    assert seen["auth"] == "Bearer s3cret"
    assert seen["sig"] == crm.sign(seen["body"], "s3cret")
    assert json.loads(seen["body"])["call_id"] == 7


@pytest.mark.asyncio
async def test_delivery_retries_then_succeeds_and_logs(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(500 if calls["n"] < 3 else 200)

    out = await crm.deliver(cfg, {"call_id": 9}, transport=httpx.MockTransport(handler),
                            backoff_s=0)
    assert out["status"] == "sent" and out["attempts"] == 3
    lines = crm.deliveries_path().read_text().strip().splitlines()
    assert [json.loads(line)["status"] for line in lines] == ["failed", "failed", "sent"]


@pytest.mark.asyncio
async def test_delivery_failure_never_raises(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)

    def handler(request):
        raise httpx.ConnectError("down", request=request)

    out = await crm.deliver(cfg, {"call_id": 3}, transport=httpx.MockTransport(handler),
                            backoff_s=0)
    assert out["status"] == "failed" and out["attempts"] == 3


def test_disabled_crm_is_not_enabled():
    cfg = load_config()
    assert crm.crm_enabled(cfg) is False
