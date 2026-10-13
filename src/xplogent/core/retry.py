"""Error classification + retry policy.

Transient failures (network blips, timeouts, rate limits) shouldn't sink a whole
task or agent turn. :func:`classify_error` buckets an exception/message, and
:class:`RetryPolicy` provides exponential backoff. The orchestrator retries failed
tasks and the agent retries transient provider errors using these.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeVar


class ErrorClass(StrEnum):
    TRANSIENT = "transient"      # connection reset / temporary network issue
    RATE_LIMIT = "rate_limit"    # HTTP 429 / "rate limit"
    TIMEOUT = "timeout"          # request/command timed out
    TOOL_ERROR = "tool_error"    # a tool reported a recoverable error
    FATAL = "fatal"              # bad request, auth, logic — don't retry


RETRYABLE = {ErrorClass.TRANSIENT, ErrorClass.RATE_LIMIT, ErrorClass.TIMEOUT}


def classify_error(err: Any) -> ErrorClass:
    """Bucket an exception or message for retry decisions."""
    # httpx is a hard dependency, but keep the import local so this module is cheap.
    try:
        import httpx

        if isinstance(err, httpx.TimeoutException):
            return ErrorClass.TIMEOUT
        if isinstance(err, (httpx.ConnectError, httpx.NetworkError,
                            httpx.RemoteProtocolError)):
            return ErrorClass.TRANSIENT
        if isinstance(err, httpx.HTTPStatusError):
            code = err.response.status_code
            if code == 429:
                return ErrorClass.RATE_LIMIT
            if code >= 500:
                return ErrorClass.TRANSIENT
            return ErrorClass.FATAL
    except Exception:  # noqa: BLE001 - httpx missing/odd; fall back to text
        pass

    if isinstance(err, (asyncio.TimeoutError, TimeoutError)):
        return ErrorClass.TIMEOUT

    text = str(err).lower()
    if "429" in text or "rate limit" in text or "too many requests" in text:
        return ErrorClass.RATE_LIMIT
    if "timed out" in text or "timeout" in text:
        return ErrorClass.TIMEOUT
    if any(s in text for s in ("connection", "temporarily", "reset by peer",
                               "network", "503", "502", "504", "529", "overloaded")):
        return ErrorClass.TRANSIENT
    return ErrorClass.FATAL


@dataclass
class RetryPolicy:
    max_attempts: int = 3        # total tries (1 initial + retries)
    base_delay: float = 1.0
    factor: float = 2.0
    max_delay: float = 30.0

    def delay_for(self, attempt: int) -> float:
        """Backoff before the *next* attempt (attempt is 1-based, already failed)."""
        return min(self.base_delay * (self.factor ** (attempt - 1)), self.max_delay)

    @classmethod
    def from_attempts(cls, retries: int) -> RetryPolicy:
        return cls(max_attempts=max(1, int(retries) + 1))


T = TypeVar("T")


async def with_retry(
    factory: Callable[[], Awaitable[T]],
    policy: RetryPolicy,
    *,
    on_retry: Callable[[int, ErrorClass, Exception], Awaitable[None]] | None = None,
) -> T:
    """Run ``factory()``; retry on retryable errors with backoff. Re-raises otherwise."""
    attempt = 0
    while True:
        attempt += 1
        try:
            return await factory()
        except Exception as exc:  # noqa: BLE001 - classification decides fate
            cls = classify_error(exc)
            if cls not in RETRYABLE or attempt >= policy.max_attempts:
                raise
            if on_retry is not None:
                await on_retry(attempt, cls, exc)
            await asyncio.sleep(policy.delay_for(attempt))
