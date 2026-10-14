"""Shared pieces for the API routers: auth/origin checks, auditing, run state.

Route modules call the factories through this module (``deps.build_runtime(...)``)
rather than importing them directly, so there is exactly one place to swap them
(tests replace them here).
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from xplogent.core.config import load_config
from xplogent.interfaces.api.security import (
    configured_hosts,
    configured_origins,
    host_allowed,
    origin_allowed,
    token_matches,
)
from xplogent.memory.store import Store
from xplogent.providers.registry import build_provider  # noqa: F401 - re-exported
from xplogent.runtime import build_runtime  # noqa: F401 - re-exported

# ── shared state ──────────────────────────────────────────────────────────────
# Fire-and-forget tasks (kept referenced so they aren't garbage-collected mid-run).
bg_tasks: set[asyncio.Task] = set()
webhook_running: set[int] = set()
WEBHOOK_MAX_BYTES = 256_000
# Args used when the server re-execs itself after an update (set by run_server).
SERVE_ARGS: list[str] = ["up"]


def spawn(coro: Any) -> asyncio.Task:
    task = asyncio.create_task(coro)
    bg_tasks.add(task)
    task.add_done_callback(bg_tasks.discard)
    return task


# ── access token + browser checks ─────────────────────────────────────────────
# When ``server.auth_token`` is set (auto-enabled when binding to a non-local host or
# behind a proxy), every API call must present it via ``Authorization: Bearer``, a
# ``xpl_token`` cookie, or a ``?token=`` query param. The dashboard shell and health
# check stay public so the browser can load and prompt for the token.
_STATIC_EXT = (".js", ".css", ".svg", ".png", ".ico", ".woff", ".woff2", ".map", ".webmanifest")
# Server-to-server callbacks: no browser Origin, authenticated by the secret in the path.
WEBHOOK_PREFIXES = ("/telephony/webhook/", "/triggers/webhook/")


_server_cache: dict[str, Any] = {"key": None, "value": {}}


def server_settings() -> dict:
    """The ``server:`` config section, re-read only when config.yaml changes.

    The auth middleware needs it on every request; a full ``load_config()`` (YAML
    parsing, .env and secret-store reads) twice per request was the main per-request
    overhead. Keyed on the file's mtime and size, so edits apply immediately.
    """
    from xplogent.core.config import xplogent_home

    user_cfg = xplogent_home() / "config.yaml"
    try:
        st = user_cfg.stat()
        key: tuple = (str(user_cfg), st.st_mtime_ns, st.st_size)
    except OSError:
        key = (str(user_cfg), None)
    if _server_cache["key"] != key:
        _server_cache["value"] = load_config().raw.get("server", {}) or {}
        _server_cache["key"] = key
    return _server_cache["value"]


def auth_token() -> str:
    # XPLOGENT_AUTH_TOKEN (set by the Docker/systemd deployments) wins over config.
    env = os.environ.get("XPLOGENT_AUTH_TOKEN", "").strip()
    if env:
        return env
    return str(server_settings().get("auth_token") or "").strip()


def is_public(path: str, method: str) -> bool:
    if path in ("/health", "/auth/check"):
        return True
    if path.startswith(WEBHOOK_PREFIXES):
        return True
    return method == "GET" and (
        path == "/" or path.startswith("/assets") or path.endswith(_STATIC_EXT))


def request_token(request: Any) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.cookies.get("xpl_token", "") or request.query_params.get("token", "")


def browser_check(headers: Any, path: str, token: str) -> str:
    """Return an error string if a request must be refused for origin/host reasons."""
    cfg_server = server_settings()
    if not host_allowed(headers.get("host"), bool(token), configured_hosts(cfg_server)):
        return "host not allowed"
    if not origin_allowed(headers.get("origin"), headers.get("host"), path,
                          configured_origins(cfg_server)):
        return "cross-origin request refused"
    return ""


def ws_authorized(websocket: Any) -> bool:
    """Same rules as HTTP: right Origin/Host, plus the token when one is set."""
    token = auth_token()
    if browser_check(websocket.headers, websocket.url.path, token):
        return False
    if not token:
        return True
    provided = (websocket.query_params.get("token", "")
                or websocket.cookies.get("xpl_token", ""))
    return token_matches(provided, token)


def audit_event(action: str, target: str = "", risk: str = "",
                allowed: bool | None = None, detail: str = "") -> None:
    try:
        with Store(load_config().db_path) as store:
            store.add_audit("dashboard", action, target, risk, allowed, detail)
    except Exception:  # noqa: BLE001 - auditing must never break a request
        pass
