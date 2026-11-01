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

import httpcore
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


async def _resolve(host: str, port: int) -> list[str]:
    try:
        ipaddress.ip_address(host)
        return [host]
    except ValueError:
        pass
    infos = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(info[4][0] for info in infos))


_REFUSED = "refused by the outbound guard"


class GuardedBackend(httpcore.AsyncNetworkBackend):
    """Resolves the host **once**, refuses if any answer is private/local, and connects to
    exactly that checked address. A rebinding name can't pass the check on one IP and then
    connect to another. TLS still uses the original name (SNI and certificate checks), as
    httpcore passes the URL's host to ``start_tls``."""

    def __init__(self, *, allow_private: bool = False, resolver=None, inner=None) -> None:
        self.allow_private = allow_private
        self.resolver = resolver or _resolve
        self.inner = inner or httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        try:
            addrs = await self.resolver(host, port)
        except OSError as exc:
            raise httpcore.ConnectError(f"cannot resolve {host}: {exc}") from exc
        if not addrs:
            raise httpcore.ConnectError(f"cannot resolve {host}")
        if not self.allow_private and not all(_is_public_ip(a) for a in addrs):
            raise httpcore.ConnectError(f"{_REFUSED}: {host} resolves to a private/local address")
        return await self.inner.connect_tcp(addrs[0], port, timeout=timeout, local_address=local_address,
                                            socket_options=socket_options)

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpcore.ConnectError(f"{_REFUSED}: unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self.inner.sleep(seconds)


class GuardedTransport(httpx.AsyncHTTPTransport):
    """An httpx transport whose every connection goes through :class:`GuardedBackend`."""

    def __init__(self, backend: GuardedBackend, **kwargs) -> None:
        super().__init__(**kwargs)
        ssl_context = getattr(self._pool, "_ssl_context", None)
        self._pool = httpcore.AsyncConnectionPool(ssl_context=ssl_context, network_backend=backend,
                                                  max_connections=10)


def _proxied() -> bool:
    """Behind an HTTP(S) proxy the proxy resolves names, so pinning isn't possible: the
    pre-connect check still applies (documented)."""
    import os

    return any(os.environ.get(k) for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                                          "ALL_PROXY", "all_proxy"))


async def safe_get(url: str, *, headers: dict[str, str] | None = None, timeout: float = 30,
                   max_bytes: int = MAX_BYTES, allow_private: bool | None = None,
                   backend: GuardedBackend | None = None) -> tuple[str, str]:
    """GET ``url`` with SSRF checks on every hop. Returns ``(final_url, text)``.

    Each connection is made to the address that passed the check (no DNS-rebinding gap).
    Raises ``ValueError`` for a refused URL and ``httpx.HTTPError`` for transport errors.
    """
    if allow_private is None:
        allow_private = _private_allowed()
    if backend is None and _proxied():
        client = httpx.AsyncClient(timeout=timeout, headers=headers or {}, follow_redirects=False)
    else:
        transport = GuardedTransport(backend or GuardedBackend(allow_private=allow_private))
        client = httpx.AsyncClient(timeout=timeout, headers=headers or {}, follow_redirects=False,
                                   transport=transport, trust_env=False)
    async with client:
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            problem = await check_url(current, allow_private=allow_private) if backend is None else \
                (None if urlsplit(current).scheme in ("http", "https") else "only http(s) URLs are allowed")
            if problem:
                raise ValueError(problem)
            try:
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
            except httpx.ConnectError as exc:
                if _REFUSED in str(exc):
                    raise ValueError(str(exc)) from exc
                raise
        raise ValueError(f"too many redirects (>{MAX_REDIRECTS})")
