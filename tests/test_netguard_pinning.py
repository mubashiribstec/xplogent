"""Audit P1: the outbound fetch guard connects to the address it checked (no DNS rebinding)."""

from __future__ import annotations

import httpcore
import pytest

from xplogent.core.netguard import GuardedBackend, safe_get

OK = [b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\nhi"]


class Recording(httpcore.AsyncMockBackend):
    def __init__(self, buffer):
        super().__init__(buffer)
        self.seen = []

    async def connect_tcp(self, host, port, *args, **kwargs):
        self.seen.append((host, port))
        return await super().connect_tcp(host, port, *args, **kwargs)


def resolver(table):
    calls = {}

    async def resolve(host, port):
        answers = table[host]
        n = calls.get(host, 0)
        calls[host] = n + 1
        return answers[min(n, len(answers) - 1)] if isinstance(answers[0], list) else answers
    return resolve


async def test_connects_to_the_checked_public_address_with_the_original_host():
    inner = Recording(OK)
    backend = GuardedBackend(resolver=resolver({"docs.example": ["93.184.216.34"]}), inner=inner)
    final, text = await safe_get("http://docs.example/page", backend=backend)
    assert text == "hi" and final == "http://docs.example/page"
    assert inner.seen == [("93.184.216.34", 80)]                  # the pinned IP, not a 2nd lookup


async def test_rebinding_name_is_refused_at_connect_time():
    # First lookup public (what a separate pre-check would see), the next private.
    resolve = resolver({"rebind.example": [["93.184.216.34"], ["127.0.0.1"]]})
    await resolve("rebind.example", 80)                            # the "check"
    backend = GuardedBackend(resolver=resolve, inner=Recording(OK))
    with pytest.raises(ValueError, match="private"):
        await safe_get("http://rebind.example/", backend=backend)
    assert backend.inner.seen == []                                # never connected


@pytest.mark.parametrize("answers", [["93.184.216.34", "10.0.0.5"], ["::ffff:127.0.0.1"], ["169.254.169.254"],
                                     ["fd00::1"]])
async def test_mixed_and_disguised_private_answers_are_refused(answers):
    backend = GuardedBackend(resolver=resolver({"x.example": answers}), inner=Recording(OK))
    with pytest.raises(ValueError, match="private"):
        await safe_get("http://x.example/", backend=backend)


async def test_redirect_to_a_private_host_is_refused():
    redirect = [b"HTTP/1.1 302 Found\r\nLocation: http://internal.example/admin\r\nContent-Length: 0\r\n\r\n"]
    backend = GuardedBackend(resolver=resolver({"pub.example": ["93.184.216.34"],
                                                "internal.example": ["10.1.2.3"]}),
                             inner=Recording(redirect))
    with pytest.raises(ValueError, match="private"):
        await safe_get("http://pub.example/", backend=backend)
    assert backend.inner.seen == [("93.184.216.34", 80)]


async def test_private_network_opt_in_still_works():
    inner = Recording(OK)
    backend = GuardedBackend(allow_private=True, resolver=resolver({"intranet": ["10.0.0.7"]}), inner=inner)
    assert (await safe_get("http://intranet/", backend=backend))[1] == "hi"
    assert inner.seen == [("10.0.0.7", 80)]


async def test_non_http_schemes_are_refused():
    with pytest.raises(ValueError, match="http"):
        await safe_get("file:///etc/passwd", backend=GuardedBackend(inner=Recording(OK)))
