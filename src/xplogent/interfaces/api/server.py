"""FastAPI server exposing Xplogent over REST + WebSocket.

REST is convenient for one-shot calls and inspection; the WebSocket streams the
full agent event log live and carries the approval round-trip (the browser
dashboard resolves ``approval_required`` events). The server is created lazily so
importing this module never requires FastAPI unless you actually serve.

This module only assembles the app: routes live in ``interfaces/api/routes/*``,
shared auth/origin checks and run state in ``interfaces/api/deps.py``.
"""

import asyncio
import os

from xplogent.core import updater
from xplogent.core.config import load_config, save_user_config
from xplogent.core.logging import get_logger
from xplogent.interfaces.api import deps

_startup_log = get_logger("startup")


def create_app():
    from contextlib import asynccontextmanager

    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse

    from xplogent.interfaces.api.routes import ALL_ROUTERS
    from xplogent.interfaces.api.security import configured_origins, token_matches

    @asynccontextmanager
    async def lifespan(app: "FastAPI"):
        cfg = load_config()
        sched = None
        if cfg.scheduler.get("enabled", True):
            from xplogent.core.scheduler import Scheduler
            sched = Scheduler(cfg, tick=float(cfg.scheduler.get("tick_seconds", 30)))
            await sched.start()
            app.state.scheduler = sched
        from xplogent.core.triggers import FileWatcher
        watcher = FileWatcher(cfg, tick=float(cfg.scheduler.get("file_tick_seconds", 5)))
        await watcher.start()
        app.state.watcher = watcher
        sip_task = None
        from xplogent.voip.config_check import check_voip
        for problem in check_voip(cfg.voip or {}):
            _startup_log.warning("settings: %s", problem)
        from xplogent.voip.telephony import sip as sip_mod
        if sip_mod.wants_sip(cfg.voip or {}):
            sip_task = asyncio.create_task(sip_mod.apply_settings(cfg))
        try:
            yield
        finally:
            if sip_task is not None:
                sip_task.cancel()
            await sip_mod.shutdown()
            if sched is not None:
                await sched.stop()
            await watcher.stop()

    app = FastAPI(title="Xplogent Agent API", version="0.1.0", lifespan=lifespan)
    # No wildcard CORS: the dashboard is served same-origin. Only explicitly configured
    # origins (server.allowed_origins / XPLOGENT_ALLOWED_ORIGINS, plus the Vite dev server)
    # may make cross-origin calls.
    _server_cfg = load_config().raw.get("server", {}) or {}
    app.add_middleware(
        CORSMiddleware,
        allow_origins=sorted(configured_origins(_server_cfg)),
        allow_methods=["*"], allow_headers=["*"], allow_credentials=True,
    )

    @app.middleware("http")
    async def _auth_mw(request, call_next):
        token = deps.auth_token()
        path = request.url.path
        # Carrier/webhook callbacks come from servers (no browser Origin) and authenticate
        # with their own path secret, so they skip the browser checks too.
        if not path.startswith(deps.WEBHOOK_PREFIXES):
            problem = deps.browser_check(request.headers, path, token)
            if problem:
                return JSONResponse({"error": problem}, status_code=403)
        if token and not deps.is_public(path, request.method):
            if not token_matches(deps.request_token(request), token):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

    for router in ALL_ROUTERS:
        app.include_router(router)

    # Serve the built dashboard (same origin); declared after the API routes.
    _mount_dashboard(app)
    return app


def _dashboard_dir():
    from pathlib import Path

    candidates = []
    if os.environ.get("XPLOGENT_WEB_DIR"):                       # explicit override (Docker)
        candidates.append(Path(os.environ["XPLOGENT_WEB_DIR"]))
    candidates.append(Path(__file__).resolve().parents[2] / "web_dist")   # packaged build
    root = updater.repo_root()
    if root:
        candidates.append(root / "web" / "dist")
    candidates.append(Path(__file__).resolve().parents[3] / "web" / "dist")
    for path in candidates:
        if path.is_dir():
            return path
    return None


def _mount_dashboard(app) -> None:
    """Mount the built React dashboard at '/' if it exists (declared after the API)."""
    dist = _dashboard_dir()
    if not dist:
        return
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=str(dist), html=True), name="dashboard")


def run_server(host: str | None = None, port: int | None = None,
               open_browser: bool = False) -> None:
    import uvicorn

    host = host or os.environ.get("XPLOGENT_API_HOST", "127.0.0.1")
    port = int(port or os.environ.get("XPLOGENT_API_PORT", 8765))
    # Keep the bind address after a self-update restart, and never pop a browser tab.
    deps.SERVE_ARGS[:] = ["up", "--port", str(port), "--host", host, "--no-browser"]

    # Security: when exposed beyond localhost, require an access token. Generate
    # and persist one automatically if the operator hasn't set their own.
    _is_local = host in ("127.0.0.1", "localhost", "::1", "")
    # Behind a reverse proxy the app binds to localhost but is still internet-facing,
    # so the deployment files set XPLOGENT_BEHIND_PROXY=1 to demand a token anyway.
    _behind_proxy = os.environ.get("XPLOGENT_BEHIND_PROXY", "").lower() in ("1", "true", "yes")
    if not _is_local or _behind_proxy:
        cfg = load_config()
        if (not os.environ.get("XPLOGENT_AUTH_TOKEN", "").strip()
                and not str(cfg.raw.get("server", {}).get("auth_token") or "").strip()):
            import secrets as _pysecrets

            token = _pysecrets.token_urlsafe(32)
            save_user_config({"server": {"auth_token": token}})
            import sys as _sys

            from xplogent.core.config import xplogent_home as _home

            print("\n  Access token enabled (exposed or behind a proxy).")
            if _sys.stdout.isatty():
                print(f"  Your dashboard token:  {token}")
            else:
                # Not a terminal (service / `xplogent start` log file): don't write the
                # secret into a log; point at where it's stored instead.
                print(f"  Token saved in {_home() / 'config.yaml'} (server.auth_token); "
                      "run `xplogent token` to rotate.")
            print("  Paste it when the dashboard prompts (Settings → re-generate to rotate).\n")

    if open_browser:
        import threading
        import webbrowser

        url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}"
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
        if _dashboard_dir() is None:
            print("Dashboard not built yet — serving API only. "
                  "Build it with: cd web && npm install && npm run build")

    uvicorn.run(create_app(), host=host, port=port, proxy_headers=True,
                forwarded_allow_ips=os.environ.get("XPLOGENT_FORWARDED_ALLOW_IPS", "127.0.0.1"))
