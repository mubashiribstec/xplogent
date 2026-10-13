"""Browser-origin protection for the local API.

Xplogent's API can run shell commands, so a web page the user happens to visit must not
be able to drive it. Two independent checks (pure functions, unit-tested):

* **Origin** — browsers attach ``Origin`` to cross-site fetches, form posts and *every*
  WebSocket handshake. A request whose Origin isn't the dashboard itself (or an explicitly
  allowed origin) is refused, which stops CSRF and cross-site WebSocket hijacking. Requests
  without an Origin (curl, carriers, the CLI) are not browser-cross-site and pass.
* **Host** — when no access token is configured the API trusts "being on localhost", so the
  ``Host`` header must name this machine. That defeats DNS-rebinding (a hostile domain that
  re-resolves to 127.0.0.1 still sends its own name as Host). With a token the token is the
  gate, so any Host is fine (servers behind a reverse proxy use their public domain).
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"}
# Vite dev server (proxies to the API but keeps the browser's Origin).
DEV_ORIGINS = {"http://localhost:5173", "http://127.0.0.1:5173"}


def _split_host(host: str) -> str:
    """Hostname part of a Host header (strips the port, keeps IPv6 brackets)."""
    host = (host or "").strip().lower()
    if host.startswith("["):
        return host.split("]")[0] + "]"
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def configured_origins(server_cfg: dict | None) -> set[str]:
    out = set(DEV_ORIGINS)
    for item in (server_cfg or {}).get("allowed_origins", []) or []:
        out.add(str(item).rstrip("/").lower())
    env = os.environ.get("XPLOGENT_ALLOWED_ORIGINS", "")
    out.update(o.strip().rstrip("/").lower() for o in env.split(",") if o.strip())
    return out


def configured_hosts(server_cfg: dict | None) -> set[str]:
    out = set(LOCAL_HOSTNAMES)
    for item in (server_cfg or {}).get("allowed_hosts", []) or []:
        out.add(_split_host(str(item)))
    env = os.environ.get("XPLOGENT_ALLOWED_HOSTS", "")
    out.update(_split_host(h) for h in env.split(",") if h.strip())
    return out


def origin_allowed(origin: str | None, host: str | None, path: str,
                   allowed_origins: set[str]) -> bool:
    """True if a request carrying ``origin`` may use the API.

    Same-origin (Origin's host:port equals the request Host) is always fine. The Chrome
    extension connects from ``chrome-extension://`` and may only use its own socket.
    """
    if not origin:
        return True
    o = origin.strip().rstrip("/").lower()
    if o in allowed_origins:
        return True
    if o.startswith(("chrome-extension://", "moz-extension://")):
        return path == "/ws/extension"
    netloc = urlsplit(o).netloc
    return bool(netloc) and netloc == (host or "").strip().lower()


def host_allowed(host: str | None, has_token: bool, allowed_hosts: set[str]) -> bool:
    if has_token:
        return True
    return _split_host(host or "") in allowed_hosts


def token_matches(provided: str, expected: str) -> bool:
    """Constant-time compare that never raises (non-ASCII input → simply False)."""
    import hmac

    return hmac.compare_digest((provided or "").encode("utf-8"), (expected or "").encode("utf-8"))
