"""Error classification + retry policy."""

from __future__ import annotations

import httpx
import pytest

from xplogent.core.retry import (
    ErrorClass,
    RetryPolicy,
    classify_error,
    with_retry,
)


def test_classify_buckets():
    assert classify_error(TimeoutError()) == ErrorClass.TIMEOUT
    assert classify_error(httpx.ConnectError("boom")) == ErrorClass.TRANSIENT
    assert classify_error(Exception("HTTP 429 rate limit")) == ErrorClass.RATE_LIMIT
    assert classify_error(Exception("connection reset by peer")) == ErrorClass.TRANSIENT
    assert classify_error(ValueError("bad argument")) == ErrorClass.FATAL


def test_policy_backoff_grows_and_caps():
    p = RetryPolicy(max_attempts=4, base_delay=1, factor=2, max_delay=5)
    assert p.delay_for(1) == 1
    assert p.delay_for(2) == 2
    assert p.delay_for(3) == 4
    assert p.delay_for(4) == 5  # capped


@pytest.mark.asyncio
async def test_with_retry_succeeds_after_transient():
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("temporary")
        return "ok"

    res = await with_retry(flaky, RetryPolicy(max_attempts=3, base_delay=0))
    assert res == "ok"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_with_retry_reraises_fatal():
    async def boom():
        raise ValueError("nope")

    with pytest.raises(ValueError):
        await with_retry(boom, RetryPolicy(max_attempts=3, base_delay=0))
