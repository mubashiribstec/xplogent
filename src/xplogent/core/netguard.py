"""Outbound-fetch guard (SSRF protection) for tools that fetch model-chosen URLs.

A prompt-injected page can ask the agent to "fetch http://169.254.169.254/..." (cloud
credentials) or "http://127.0.0.1:8765/backup?include_secrets=true". Fetch tools therefore
only reach public addresses, re-check every redirect hop, and cap the body size.
Set ``tools.allow_private_network: true`` in config to opt out (e.g. local docs servers).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import httpx

MAX_BYTES = 2_000_000
MAX_REDIRECTS = 5


def _private_allowed() -> bool:
    try:
        from xplogent.core.config import load_config

        return bool((load_config().tools or {}).get("allow_private_network", False))
    except Exception:  # noqa: BLE001
        return False


def _is_public_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_global and not addr.is_multicast


async def check_url(url: str, *, allow_private: bool | None = None) -> str | None:
    """Return an error message if ``url`` must not be fetched, else None."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return f"only http(s) URLs are allowed (got '{parts.scheme or 'none'}')"
    host = parts.hostname
    if not host:
        return "URL has no host"
    if allow_private is None:
        allow_private = _private_allowed()
    if allow_private:
        return None
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, parts.port or 443,
                                        type=socket.SOCK_STREAM)
    except OSError as exc:
        return f"cannot resolve {host}: {exc}"
    addrs = {info[4][0] for info in infos}
    if not addrs or not all(_is_public_ip(a) for a in addrs):
        return (f"refusing to fetch {host}: it resolves to a private/local address "
                "(set tools.allow_private_network to allow)")
    return None


async def safe_get(url: str, *, headers: dict[str, str] | None = None, timeout: float = 30,
                   max_bytes: int = MAX_BYTES, allow_private: bool | None = None) -> tuple[str, str]:
    """GET ``url`` with SSRF checks on every hop. Returns ``(final_url, text)``.

    Raises ``ValueError`` for a refused URL and ``httpx.HTTPError`` for transport errors.
    """
    async with httpx.AsyncClient(timeout=timeout, headers=headers or {},
                                 follow_redirects=False) as client:
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            problem = await check_url(current, allow_private=allow_private)
            if problem:
                raise ValueError(problem)
            async with client.stream("GET", current) as resp:
                if resp.is_redirect and resp.headers.get("location"):
                    current = urljoin(current, resp.headers["location"])
                    continue
                resp.raise_for_status()
                chunks: list[bytes] = []
                size = 0
                async for chunk in resp.aiter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= max_bytes:
                        break
                body = b"".join(chunks)[:max_bytes]
                return current, body.decode(resp.encoding or "utf-8", errors="replace")
        raise ValueError(f"too many redirects (>{MAX_REDIRECTS})")
