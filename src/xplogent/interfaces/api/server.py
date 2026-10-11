"""FastAPI server exposing Xplogent over REST + WebSocket.

REST is convenient for one-shot calls and inspection; the WebSocket streams the
full agent event log live and carries the approval round-trip (the browser
dashboard resolves ``approval_required`` events). The server is created lazily so
importing this module never requires FastAPI unless you actually serve.
"""

# NOTE: no `from __future__ import annotations` here — FastAPI/Pydantic must see
# the real request-model classes (defined inside create_app) to build bodies.

import asyncio
import os
import uuid

from xplogent.core import guide, updater
from xplogent.core.config import (
    load_config,
    save_env,
    save_user_config,
    secret_status,
)
from xplogent.core.events import EventBus
from xplogent.core.orchestrator import AgentSpec
from xplogent.memory.manager import MemoryManager
from xplogent.memory.store import Store
from xplogent.memory.vector import Embedder
from xplogent.monitor.recorder import TraceRecorder
from xplogent.providers.registry import available_providers, build_provider
from xplogent.runtime import build_orchestrator, build_runtime
from xplogent.safety.approval import ApprovalRequest
from xplogent.tools.registry import _BUILTIN_GROUPS


def create_app():
    from contextlib import asynccontextmanager

    from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
    from fastapi.middleware.cors import CORSMiddleware
    from pydantic import BaseModel

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
        try:
            yield
        finally:
            if sched is not None:
                await sched.stop()
            await watcher.stop()

    import hmac

    from fastapi.responses import JSONResponse

    app = FastAPI(title="Xplogent Agent API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
    )

    # ── Optional access-token gate ───────────────────────────────────────────
    # When ``server.auth_token`` is set (auto-enabled when binding to a non-local
    # host), every API call must present it via ``Authorization: Bearer``, a
    # ``xpl_token`` cookie, or a ``?token=`` query param. The dashboard shell and
    # health check stay public so the browser can load and prompt for the token.
    _STATIC_EXT = (".js", ".css", ".svg", ".png", ".ico", ".woff", ".woff2", ".map", ".webmanifest")

    def _auth_token() -> str:
        return str(load_config().raw.get("server", {}).get("auth_token") or "").strip()

    def _is_public(path: str, method: str) -> bool:
        if path in ("/health", "/auth/check"):
            return True
        # Inbound webhooks authenticate via their secret token in the path.
        if path.startswith("/triggers/webhook/"):
            return True
        # Carrier call webhooks authenticate with the secret in the path.
        if path.startswith("/telephony/webhook/"):
            return True
        return method == "GET" and (
            path == "/" or path.startswith("/assets") or path.endswith(_STATIC_EXT))

    def _request_token(request) -> str:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return request.cookies.get("xpl_token", "") or request.query_params.get("token", "")

    @app.middleware("http")
    async def _auth_mw(request, call_next):
        token = _auth_token()
        if token and not _is_public(request.url.path, request.method):
            if not hmac.compare_digest(_request_token(request), token):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

    def _audit_event(action: str, target: str = "", risk: str = "",
                     allowed: bool | None = None, detail: str = "") -> None:
        try:
            store = Store(load_config().db_path)
            store.add_audit("dashboard", action, target, risk, allowed, detail)
            store.close()
        except Exception:  # noqa: BLE001 - auditing must never break a request
            pass

    class RunRequest(BaseModel):
        task: str

    class AgentSpecModel(BaseModel):
        name: str
        role: str = "operator"
        task: str = ""
        model: str | None = None

    class OrchestrateRequest(BaseModel):
        goal: str | None = None
        specs: list[AgentSpecModel] | None = None
        max_concurrent: int | None = None
        mode: str = "auto"
        auto_approve: bool = True

    class ConfigPatch(BaseModel):
        updates: dict = {}

    class SecretsPatch(BaseModel):
        keys: dict[str, str] = {}

    class RolePatch(BaseModel):
        allowed_tools: object = "*"
        policy: dict = {}
        allowed_paths: list[str] = []
        network: bool = True
        max_steps: int = 25

    class RenameBody(BaseModel):
        title: str

    class FactBody(BaseModel):
        content: str

    class TextBody(BaseModel):
        content: str = ""

    class ScheduleBody(BaseModel):
        name: str = ""
        prompt: str
        schedule: str           # e.g. "every day at 9am" or a 5-field cron
        mode: str = "agent"     # "agent" | "team"
        tz: str = ""

    # Live multi-agent runs, keyed by run_id, for monitoring + control.
    active_runs: dict[str, dict] = {}

    @app.get("/health")
    async def health() -> dict:
        cfg = load_config()
        return {"status": "ok", "model": cfg.model, "providers": available_providers(),
                "auth": bool(_auth_token())}

    @app.get("/auth/check")
    async def auth_check(request: Request) -> dict:
        """Public: tells the dashboard whether auth is on and whether a token is valid."""
        token = _auth_token()
        if not token:
            return {"required": False, "ok": True}
        ok = hmac.compare_digest(_request_token(request), token)
        return {"required": True, "ok": ok}

    @app.get("/status")
    async def status() -> dict:
        """Aggregate health for the dashboard: providers, which keys are set, Ollama up."""
        import os

        import httpx
        cfg = load_config()
        ollama_host = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
        ollama_up = False
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                ollama_up = (await client.get(f"{ollama_host}/api/tags")).status_code == 200
        except httpx.HTTPError:
            ollama_up = False
        return {
            "status": "ok",
            "model": cfg.model,
            "providers": available_providers(),
            "secrets": secret_status(),
            "ollama": {"host": ollama_host, "reachable": ollama_up},
        }

    @app.post("/providers/ollama/pull")
    async def ollama_pull(body: dict) -> dict:
        """Pull a local Ollama model (used by onboarding). Best-effort, blocking."""
        import shutil
        import subprocess

        model = str(body.get("model", "")).strip()
        if not model:
            return {"ok": False, "error": "no model given"}
        if shutil.which("ollama") is None:
            return {"ok": False, "error": "the 'ollama' CLI isn't installed"}

        def _pull() -> tuple[int, str]:
            proc = subprocess.run(["ollama", "pull", model],
                                  capture_output=True, text=True, timeout=1800)
            return proc.returncode, (proc.stdout + proc.stderr)[-2000:]

        rc, out = await asyncio.to_thread(_pull)
        return {"ok": rc == 0, "output": out}

    @app.get("/providers/{name}/models")
    async def provider_models(name: str) -> dict:
        """Fetch a provider's live model catalog (using its configured key/host)."""
        if name not in available_providers():
            return {"models": [], "error": f"unknown provider '{name}'"}
        try:
            prov = build_provider(f"{name}:_")
        except Exception as exc:  # noqa: BLE001
            return {"models": [], "error": str(exc)}
        error = ""
        try:
            models = await prov.list_models()
        except Exception as exc:  # noqa: BLE001 - no key / network / 401 → surface, don't crash
            models, error = [], str(exc)
        finally:
            await prov.aclose()
        if not models and not error:
            error = f"no models returned (is the {name} API key set?)"
        return {"models": sorted(models), "error": error}

    # ── Vision: one-click local model + self-test ────────────────────────────
    @app.post("/vision/enable-local")
    async def vision_enable_local(body: dict | None = None) -> dict:
        """Pull a local vision model (default llava) and set it as the vision model."""
        import shutil
        import subprocess

        model = str((body or {}).get("model", "") or "llava").strip()
        if shutil.which("ollama") is None:
            return {"ok": False, "error": "the 'ollama' CLI isn't installed"}

        def _pull() -> tuple[int, str]:
            proc = subprocess.run(["ollama", "pull", model],
                                  capture_output=True, text=True, timeout=1800)
            return proc.returncode, (proc.stdout + proc.stderr)[-2000:]

        rc, out = await asyncio.to_thread(_pull)
        if rc != 0:
            return {"ok": False, "error": f"ollama pull failed: {out[-400:]}"}
        save_user_config({"vision_model": f"ollama:{model}"})
        return {"ok": True, "vision_model": f"ollama:{model}", "output": out}

    @app.post("/vision/test")
    async def vision_test() -> dict:
        """Run analyze_image on a tiny generated image to verify the vision model."""
        import base64
        import tempfile

        from xplogent.tools.vision import AnalyzeImageTool

        cfg = load_config()
        model = (cfg.vision_model or "").strip() or cfg.model
        # A minimal 1x1 red PNG — enough to confirm the vision pipeline responds.
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
            fh.write(png)
            path = fh.name
        res = await AnalyzeImageTool().run(path=path, question="What color is this image?")
        return {"ok": res.ok, "model": model,
                "reply": res.output if res.ok else res.error}

    @app.get("/config")
    async def config() -> dict:
        cfg = load_config()
        return {
            "model": cfg.model,
            "reflection_model": cfg.reflection_model,
            "embedding_model": cfg.embedding_model,
            "tools_enabled": cfg.tools.get("enabled", []),
            "providers": available_providers(),
        }

    @app.get("/skills")
    async def skills() -> dict:
        cfg = load_config()
        store = Store(cfg.db_path)
        out = [
            {"name": s.name, "description": s.description, "uses": s.uses,
             "level": s.level, "stars": s.stars, "successes": s.successes,
             "failures": s.failures, "trigger": s.trigger, "source": s.source}
            for s in store.all_skills()
        ]
        store.close()
        return {"skills": out}

    @app.get("/memory/search")
    async def memory_search(q: str) -> dict:
        cfg = load_config()
        store = Store(cfg.db_path)
        facts = [f.content for f in store.all_facts() if q.lower() in f.content.lower()]
        msgs = store.search_messages(q, limit=10)
        store.close()
        return {"facts": facts, "messages": msgs}

    @app.post("/run")
    async def run(req: RunRequest) -> dict:
        """One-shot run. Confirmation-tier tools are blocked (no interactive approver)."""
        bus = EventBus()
        runtime = build_runtime(bus=bus)
        answer = await runtime.agent.run(req.task)
        await runtime.aclose()
        return {"answer": answer}

    # ── Chat sessions (persistent history) ───────────────────────────────────
    @app.get("/sessions")
    async def list_sessions() -> dict:
        store = Store(load_config().db_path)
        out = store.list_sessions()
        store.close()
        return {"sessions": out}

    @app.post("/sessions")
    async def new_session() -> dict:
        store = Store(load_config().db_path)
        sid = store.create_session(title="chat")
        store.close()
        return {"id": sid}

    @app.get("/sessions/{session_id}/messages")
    async def session_messages(session_id: int) -> dict:
        store = Store(load_config().db_path)
        out = store.session_messages(session_id)
        store.close()
        return {"messages": out}

    @app.patch("/sessions/{session_id}")
    async def rename_session(session_id: int, body: RenameBody) -> dict:
        store = Store(load_config().db_path)
        store.rename_session(session_id, body.title)
        store.close()
        return {"ok": True}

    @app.delete("/sessions/{session_id}")
    async def delete_session(session_id: int) -> dict:
        store = Store(load_config().db_path)
        store.delete_session(session_id)
        store.close()
        return {"ok": True}

    @app.post("/sessions/{session_id}/undo")
    async def undo_session(session_id: int, n: int = 1) -> dict:
        """Roll back the last n user→assistant exchanges in a session."""
        store = Store(load_config().db_path)
        removed = store.delete_last_turns(session_id, n)
        store.close()
        return {"ok": True, "removed": removed}

    # ── Model presets ────────────────────────────────────────────────────────
    @app.get("/models")
    async def models() -> dict:
        cfg = load_config()
        return {"models": cfg.models, "active": cfg.model, "providers": available_providers()}

    def _ws_authorized(websocket: WebSocket) -> bool:
        token = _auth_token()
        if not token:
            return True
        provided = (websocket.query_params.get("token", "")
                    or websocket.cookies.get("xpl_token", ""))
        return hmac.compare_digest(provided, token)

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        if not _ws_authorized(websocket):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        bus = EventBus()
        loop = asyncio.get_event_loop()
        pending: dict[str, asyncio.Future] = {}

        async def approve(reqo: ApprovalRequest) -> bool:
            approval_id = uuid.uuid4().hex[:8]
            fut: asyncio.Future = loop.create_future()
            pending[approval_id] = fut
            await websocket.send_json({
                "type": "approval_required", "id": approval_id,
                "tool": reqo.tool, "risk": reqo.risk.value,
                "reason": reqo.reason, "arguments": reqo.arguments,
            })
            try:
                return await asyncio.wait_for(fut, timeout=300)
            except TimeoutError:
                return False

        # Persistent session: reuse the one the client passes, else create one.
        cfg = load_config()
        qs_session = websocket.query_params.get("session_id")
        session_id = int(qs_session) if qs_session and qs_session.isdigit() else None
        if session_id is None:
            s = Store(cfg.db_path)
            session_id = s.create_session(title="chat")
            s.close()
        await websocket.send_json({"type": "session", "id": session_id})

        # One runtime at a time; rebuilt (and history-reloaded) on model switch.
        current = {"model": None, "runtime": None}

        async def get_runtime(model: str | None, gen_params: dict):
            model = model or cfg.model
            if current["runtime"] is None or current["model"] != model:
                if current["runtime"] is not None:
                    await current["runtime"].aclose()
                current["runtime"] = build_runtime(
                    bus=bus, approve=approve, model=model,
                    gen_params=gen_params, session_id=session_id,
                )
                current["model"] = model
            else:
                current["runtime"].agent.gen_params = gen_params
            return current["runtime"]

        async def forward() -> None:
            async for ev in bus.stream():
                await websocket.send_json({"type": ev.type.value, **ev.data})

        forwarder = asyncio.create_task(forward())

        running = {"on": False}

        async def handle_task(msg: dict) -> None:
            running["on"] = True
            try:
                gen_params = {k: msg[k] for k in ("effort", "thinking", "max_tokens", "temperature")
                              if msg.get(k) is not None}
                images = msg.get("images") or None
                model = msg.get("model")
                # Images need a model that can see: route image turns to the configured
                # vision model unless the user explicitly picked one.
                if images and not model:
                    vm = (load_config().vision_model or "").strip()
                    if vm:
                        model = vm
                runtime = await get_runtime(model, gen_params)
                await runtime.agent.run(msg.get("task", ""), images=images)
            except Exception as exc:  # noqa: BLE001 - never leave the chat stuck
                try:
                    await websocket.send_json({"type": "error", "message": f"run failed: {exc}"})
                except Exception:  # noqa: BLE001
                    pass
            finally:
                running["on"] = False
                try:
                    await websocket.send_json({"type": "done"})
                except Exception:  # noqa: BLE001
                    pass

        async def handle_council(msg: dict) -> None:
            """Ask several models the same question at once, then synthesize."""
            from xplogent.providers.base import Message, Role, StreamKind

            models = [m for m in msg.get("models", []) if m]
            task = msg.get("task", "")
            base = [Message(role=Role.SYSTEM, content="You are a helpful assistant."),
                    Message(role=Role.USER, content=task)]
            answers: dict[str, str] = {}

            async def run_one(model: str) -> None:
                provider = build_provider(model)
                parts: list[str] = []
                try:
                    async for ev in provider.stream(base):
                        if ev.kind == StreamKind.TOKEN:
                            parts.append(ev.text)
                            await websocket.send_json({"type": "council_token",
                                                       "channel": model, "text": ev.text})
                        elif ev.kind == StreamKind.DONE and ev.message and not parts:
                            parts.append(ev.message.content)
                            await websocket.send_json({"type": "council_token",
                                                       "channel": model,
                                                       "text": ev.message.content})
                except Exception as exc:  # noqa: BLE001 - one model failing must not sink the rest
                    await websocket.send_json({"type": "council_token", "channel": model,
                                               "text": f"[error: {exc}]"})
                finally:
                    await provider.aclose()
                answers[model] = "".join(parts)
                await websocket.send_json({"type": "council_done", "channel": model})

            await asyncio.gather(*[run_one(m) for m in models])

            synthesis = ""
            if msg.get("synthesize", True) and answers:
                synth_model = msg.get("synth_model") or models[0]
                merged = "\n\n".join(f"[{m}]\n{a}" for m, a in answers.items())
                sp = ("You are given answers from multiple AI models to the same question. "
                      "Produce the single best, most accurate combined answer, noting any "
                      f"important disagreements.\n\nQuestion:\n{task}\n\nAnswers:\n{merged}")
                provider = build_provider(synth_model)
                try:
                    async for ev in provider.stream(
                            [Message(role=Role.SYSTEM, content="You synthesize multiple answers."),
                             Message(role=Role.USER, content=sp)]):
                        if ev.kind == StreamKind.TOKEN:
                            synthesis += ev.text
                            await websocket.send_json({"type": "council_token",
                                                       "channel": "synthesis", "text": ev.text})
                finally:
                    await provider.aclose()
                await websocket.send_json({"type": "council_done", "channel": "synthesis"})

            # Persist the turn so it appears in history.
            if session_id is not None:
                store = Store(cfg.db_path)
                store.add_message(session_id, "user", task)
                store.set_session_title(session_id, task[:60])
                store.add_message(session_id, "assistant",
                                  synthesis or "\n\n".join(f"**{m}**: {a}" for m, a in answers.items()))
                store.close()
            await websocket.send_json({"type": "done"})

        async def guarded(coro) -> None:
            """Run a handler so it always ends the turn, even on failure."""
            try:
                await coro
            except Exception as exc:  # noqa: BLE001
                for payload in ({"type": "error", "message": str(exc)}, {"type": "done"}):
                    try:
                        await websocket.send_json(payload)
                    except Exception:  # noqa: BLE001
                        pass

        async def keepalive() -> None:
            # Ping the client periodically so idle proxies don't drop a long turn.
            while True:
                await asyncio.sleep(20)
                try:
                    await websocket.send_json({"type": "ping"})
                except Exception:  # noqa: BLE001
                    return

        pinger = asyncio.create_task(keepalive())

        try:
            while True:
                msg = await websocket.receive_json()
                kind = msg.get("type")
                if kind == "steer" or (kind == "task" and running["on"]):
                    # Mid-run suggestion: fold it into the active run instead of
                    # starting a second one on the shared runtime.
                    rt = current.get("runtime")
                    if rt is not None:
                        rt.agent.inject(str(msg.get("text") or msg.get("task") or ""))
                elif kind == "task" and len([m for m in msg.get("models", []) if m]) > 1:
                    asyncio.create_task(guarded(handle_council(msg)))
                elif kind == "task":
                    asyncio.create_task(handle_task(msg))
                elif kind == "cancel":
                    rt = current.get("runtime")
                    if rt is not None:
                        rt.agent.cancel()
                elif kind == "approval":
                    fut = pending.pop(msg.get("id", ""), None)
                    if fut and not fut.done():
                        fut.set_result(bool(msg.get("allowed")))
        except WebSocketDisconnect:
            pass
        finally:
            pinger.cancel()
            await bus.close()
            forwarder.cancel()
            if current["runtime"] is not None:
                await current["runtime"].aclose()

    # ── Computer-use operator ────────────────────────────────────────────────
    @app.websocket("/ws/operator")
    async def ws_operator(websocket: WebSocket) -> None:
        if not _ws_authorized(websocket):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        bus = EventBus()
        loop = asyncio.get_event_loop()
        pending: dict[str, asyncio.Future] = {}
        runtime_box: dict = {"rt": None}

        async def approve(reqo: ApprovalRequest) -> bool:
            approval_id = uuid.uuid4().hex[:8]
            fut: asyncio.Future = loop.create_future()
            pending[approval_id] = fut
            await websocket.send_json({
                "type": "approval_required", "id": approval_id, "tool": reqo.tool,
                "risk": reqo.risk.value, "reason": reqo.reason, "arguments": reqo.arguments,
            })
            try:
                return await asyncio.wait_for(fut, timeout=300)
            except TimeoutError:
                return False

        async def forward() -> None:
            async for ev in bus.stream():
                await websocket.send_json({"type": ev.type.value, **ev.data})

        forwarder = asyncio.create_task(forward())

        async def handle(msg: dict) -> None:
            from xplogent.core.operator import build_operator

            rt = build_operator(bus=bus, approve=approve,
                                 max_steps=int(msg.get("max_steps", 30)))
            runtime_box["rt"] = rt
            try:
                await rt.agent.run(str(msg.get("goal", "")))
            finally:
                await rt.aclose()
                runtime_box["rt"] = None
            await websocket.send_json({"type": "done"})

        try:
            while True:
                msg = await websocket.receive_json()
                kind = msg.get("type")
                if kind == "start":
                    asyncio.create_task(handle(msg))
                elif kind == "cancel" and runtime_box["rt"] is not None:
                    runtime_box["rt"].agent.cancel()
                elif kind == "approval":
                    fut = pending.pop(msg.get("id", ""), None)
                    if fut and not fut.done():
                        fut.set_result(bool(msg.get("allowed")))
        except WebSocketDisconnect:
            pass
        finally:
            await bus.close()
            forwarder.cancel()
            if runtime_box["rt"] is not None:
                await runtime_box["rt"].aclose()

    @app.get("/operator/screen")
    async def operator_screen():
        """Serve the most recent screenshot the operator captured (for a live preview)."""
        from fastapi.responses import FileResponse, Response

        from xplogent.core.config import xplogent_home

        shots = sorted(xplogent_home().glob("screenshot_*.png"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        if not shots:
            return Response(status_code=204)
        return FileResponse(str(shots[0]), media_type="image/png",
                            headers={"Cache-Control": "no-store"})

    # ── Phone calling agent (VoIP) ───────────────────────────────────────────
    @app.post("/telephony/webhook/{secret}")
    async def telephony_webhook(secret: str, request: Request):
        from fastapi.responses import JSONResponse

        from xplogent.voip.service import get_service

        expected = str((load_config().voip or {}).get("webhook_secret", ""))
        if not expected or not hmac.compare_digest(secret, expected):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "bad json"}, status_code=400)
        return await get_service().handle_webhook(payload)

    @app.websocket("/ws/telephony/{token}")
    async def ws_telephony(websocket: WebSocket, token: str) -> None:
        # The carrier can't send our dashboard token; the one-time stream token
        # (issued only after a verified webhook) authorises this socket instead.
        from xplogent.voip.service import get_service

        await get_service().run_media_socket(websocket, token)

    @app.get("/voip/status")
    async def voip_status() -> dict:
        from xplogent.voip.service import get_service

        cfg = load_config()
        v = cfg.voip or {}
        store = Store(cfg.db_path)
        out = {"enabled": bool(v.get("enabled")), "public_url": v.get("public_url", ""),
               "has_secret": bool(v.get("webhook_secret")),
               "webhook_path": "/telephony/webhook/<your webhook_secret>",
               "contacts": store.count_contacts(), "calls": len(store.list_calls(1000)),
               "active": [{"call_id": k, **val} for k, val in get_service().active.items()]}
        store.close()
        return out

    @app.get("/voip/calls")
    async def voip_calls(limit: int = 100) -> dict:
        store = Store(load_config().db_path)
        out = store.list_calls(limit)
        store.close()
        return {"calls": out}

    @app.get("/voip/calls/{call_id}")
    async def voip_call(call_id: int) -> dict:
        store = Store(load_config().db_path)
        out = store.get_call(call_id)
        store.close()
        return {"call": out}

    @app.get("/voip/contacts")
    async def voip_contacts(q: str = "", limit: int = 500) -> dict:
        store = Store(load_config().db_path)
        out = store.list_contacts(limit, q)
        total = store.count_contacts()
        store.close()
        return {"contacts": out, "total": total}

    @app.post("/voip/contacts/import")
    async def voip_import(body: dict) -> dict:
        from xplogent.voip.contacts import import_csv

        store = Store(load_config().db_path)
        res = import_csv(store, str(body.get("csv", "")), source=str(body.get("source", "csv")))
        store.close()
        _audit_event("contacts_import", target=f"{res.get('imported', 0)} contacts", risk="medium")
        return res

    @app.delete("/voip/contacts/{contact_id}")
    async def voip_delete_contact(contact_id: int) -> dict:
        store = Store(load_config().db_path)
        store.delete_contact(contact_id)
        store.close()
        return {"ok": True}

    @app.post("/voip/dnc")
    async def voip_add_dnc(body: dict) -> dict:
        store = Store(load_config().db_path)
        store.add_dnc(str(body.get("phone", "")), str(body.get("reason", "manual")))
        store.close()
        return {"ok": True}

    # ── Chrome extension bridge (real-browser control + monitoring) ──────────
    @app.websocket("/ws/extension")
    async def ws_extension(websocket: WebSocket) -> None:
        if not _ws_authorized(websocket):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        from xplogent.core.extension import get_bridge

        bridge = get_bridge()
        bridge.attach(websocket)
        try:
            while True:
                msg = await websocket.receive_json()
                kind = msg.get("type")
                if kind == "snapshot":
                    bridge.update_snapshot(msg.get("tabs"), msg.get("inputs"))
                elif kind == "result":
                    bridge.resolve(str(msg.get("id", "")), bool(msg.get("ok")), msg.get("data"))
                elif kind == "ping":
                    bridge.mark_seen()
                    await websocket.send_json({"type": "pong"})
        except WebSocketDisconnect:
            pass
        finally:
            bridge.detach(websocket)

    @app.get("/extension/status")
    async def extension_status() -> dict:
        from xplogent.core.extension import get_bridge
        return get_bridge().status()

    # ── Multi-agent orchestration + deep monitoring ──────────────────────────
    @app.post("/orchestrate")
    async def orchestrate(req: OrchestrateRequest) -> dict:
        """Launch a multi-agent run in the background; returns its run_id.

        Connect to ``/ws/monitor?run_id=<id>`` to watch it live, or poll
        ``/runs/{id}`` and ``/agents``.
        """
        mbus = EventBus()

        # Without a human at the keyboard, confirm-tier tools would be blocked.
        # Auto-approve up to high risk so agents can actually run shell/python/web;
        # critical ops stay denied by the role policy + deny-list.
        approve = None
        if req.auto_approve:
            from xplogent.safety.approval import RiskLevel

            async def approve(reqo: ApprovalRequest) -> bool:  # noqa: F811
                return reqo.risk != RiskLevel.CRITICAL

        runtime = build_orchestrator(bus=mbus, approve=approve)
        orch = runtime.orchestrator
        recorder = TraceRecorder(mbus, runtime.store, orch.run_id)
        recorder.start()

        async def go() -> None:
            try:
                if req.specs:
                    specs = [AgentSpec(**s.model_dump()) for s in req.specs]
                    await orch.run_team(specs, max_concurrent=req.max_concurrent)
                else:
                    await orch.run_goal(req.goal or "", max_concurrent=req.max_concurrent,
                                        mode=req.mode)
            finally:
                await mbus.close()
                await recorder.stop()
                await runtime.aclose()
                active_runs.pop(orch.run_id, None)

        task = asyncio.create_task(go())
        active_runs[orch.run_id] = {
            "orchestrator": orch, "recorder": recorder, "bus": mbus, "task": task,
        }
        return {"run_id": orch.run_id, "mode": "manual" if req.specs else req.mode}

    @app.get("/runs")
    async def runs() -> dict:
        store = Store(load_config().db_path)
        out = store.list_runs()
        store.close()
        return {"runs": out, "active": list(active_runs)}

    @app.get("/runs/{run_id}")
    async def run_detail(run_id: str) -> dict:
        store = Store(load_config().db_path)
        info = store.get_run(run_id)
        store.close()
        live = active_runs.get(run_id)
        metrics = live["recorder"].snapshot() if live else []
        return {"run": info, "metrics": metrics}

    @app.get("/runs/{run_id}/events")
    async def run_events(run_id: str) -> dict:
        store = Store(load_config().db_path)
        out = store.run_events(run_id)
        store.close()
        return {"events": out}

    @app.get("/agents")
    async def agents() -> dict:
        live = []
        for rid, info in active_runs.items():
            for a in info["orchestrator"].live_agents():
                live.append({"run_id": rid, **a})
        return {"agents": live}

    @app.get("/messages")
    async def messages(run_id: str | None = None) -> dict:
        store = Store(load_config().db_path)
        out = store.list_agent_messages(run_id)
        store.close()
        return {"messages": out}

    @app.post("/agents/{agent_id}/{action}")
    async def control_agent(agent_id: str, action: str) -> dict:
        if action not in {"pause", "resume", "cancel"}:
            return {"ok": False, "error": "unknown action"}
        for info in active_runs.values():
            if info["orchestrator"].control(agent_id, action):
                return {"ok": True}
        return {"ok": False, "error": "agent not found"}

    @app.websocket("/ws/monitor")
    async def ws_monitor(websocket: WebSocket) -> None:
        if not _ws_authorized(websocket):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        run_id = websocket.query_params.get("run_id")
        info = active_runs.get(run_id) if run_id else (
            next(iter(active_runs.values()), None)
        )
        if not info:
            await websocket.send_json({"type": "error", "message": "no such run"})
            await websocket.close()
            return
        queue = info["bus"].subscribe()
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                await websocket.send_json({"type": event.type.value, **event.data})
        except WebSocketDisconnect:
            pass
        finally:
            info["bus"].unsubscribe(queue)

    # ── Settings: full config, edits, secrets, tools, roles ──────────────────
    @app.get("/config/full")
    async def config_full() -> dict:
        cfg = load_config()
        return {
            "model": cfg.model,
            "reflection_model": cfg.reflection_model,
            "embedding_model": cfg.embedding_model,
            "vision_model": cfg.vision_model,
            "memory": cfg.memory,
            "safety": cfg.safety,
            "orchestrator": cfg.orchestrator,
            "execution": cfg.execution,
            "roles": cfg.roles,
            "mcp": cfg.mcp,
            "tools_enabled": cfg.tools.get("enabled", []),
            "models": cfg.models,
            "providers": available_providers(),
            "secrets": secret_status(),
        }

    @app.patch("/config")
    async def patch_config(body: ConfigPatch) -> dict:
        merged = save_user_config(body.updates)
        _audit_event("config_change", target=",".join(body.updates.keys())[:200])
        return {"ok": True, "config": merged}

    @app.put("/secrets")
    async def put_secrets(body: SecretsPatch) -> dict:
        save_env(body.keys)
        _audit_event("secret_change", target=",".join(body.keys.keys()), risk="high")
        return {"ok": True, "secrets": secret_status()}

    @app.get("/tools")
    async def tools() -> dict:
        cfg = load_config()
        enabled = set(cfg.tools.get("enabled", []))
        out = []
        for group, factory in _BUILTIN_GROUPS.items():
            for tool in factory():
                out.append({
                    "name": tool.name, "group": group,
                    "description": tool.description, "risk": tool.risk.value,
                    "enabled": group in enabled,
                })
        return {"tools": out, "groups": list(_BUILTIN_GROUPS)}

    @app.get("/roles")
    async def roles() -> dict:
        return {"roles": load_config().roles}

    @app.put("/roles/{name}")
    async def put_role(name: str, body: RolePatch) -> dict:
        merged = save_user_config({"roles": {name: body.model_dump()}})
        return {"ok": True, "roles": merged.get("roles", {})}

    # ── Memory & skills management ───────────────────────────────────────────
    @app.get("/memory/facts")
    async def list_facts() -> dict:
        store = Store(load_config().db_path)
        out = [{"id": f.id, "content": f.content, "source": f.source} for f in store.all_facts()]
        store.close()
        return {"facts": out}

    @app.post("/memory/facts")
    async def add_fact(body: FactBody) -> dict:
        cfg = load_config()
        store = Store(cfg.db_path)
        embed_provider = build_provider(cfg.embedding_model)
        mem = MemoryManager(store, Embedder(embed_provider))
        try:
            fact_id = await mem.remember(body.content, source="gui")
        finally:
            await embed_provider.aclose()
            store.close()
        return {"ok": True, "id": fact_id}

    @app.delete("/memory/facts/{fact_id}")
    async def delete_fact(fact_id: int) -> dict:
        store = Store(load_config().db_path)
        store.delete_fact(fact_id)
        store.close()
        return {"ok": True}

    @app.delete("/skills/{name}")
    async def delete_skill(name: str) -> dict:
        store = Store(load_config().db_path)
        store.delete_skill(name)
        store.close()
        return {"ok": True}

    @app.get("/skills/library")
    async def skills_library() -> dict:
        from xplogent.skills.hub import list_bundled
        return {"packs": list_bundled()}

    @app.post("/skills/install")
    async def skills_install(body: dict) -> dict:
        from xplogent.skills.hub import install_pack, install_text
        cfg = load_config()
        store = Store(cfg.db_path)
        embed_provider = build_provider(cfg.embedding_model)
        mem = MemoryManager(store, Embedder(embed_provider))
        try:
            if body.get("skill_md"):
                res = await install_text(str(body["skill_md"]), mem)
            elif body.get("src"):
                res = await install_pack(str(body["src"]), mem)
            else:
                res = {"ok": False, "error": "provide 'src' (path/url/pack) or 'skill_md'"}
            for nm in (res.get("installed") or []) if isinstance(res, dict) else []:
                store.add_skill_event(str(nm), "installed", "novice", 1, "installed from hub")
        except Exception as exc:  # noqa: BLE001
            res = {"ok": False, "error": str(exc)}
        finally:
            await embed_provider.aclose()
            store.close()
        return res

    # ── Scheduler (recurring / timed jobs) ───────────────────────────────────
    @app.get("/schedules")
    async def list_schedules() -> dict:
        store = Store(load_config().db_path)
        out = store.list_schedules()
        store.close()
        return {"schedules": out}

    @app.post("/schedules")
    async def add_schedule(body: ScheduleBody) -> dict:
        from xplogent.core.scheduler import parse_schedule

        try:
            spec, next_run = parse_schedule(body.schedule, body.tz)
        except ValueError as exc:
            from fastapi import HTTPException
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        store = Store(load_config().db_path)
        sid = store.add_schedule(body.name or body.prompt[:40], body.prompt,
                                 body.mode, spec, body.tz, next_run)
        store.close()
        return {"ok": True, "id": sid, "spec": spec, "next_run": next_run}

    @app.post("/schedules/{schedule_id}/toggle")
    async def toggle_schedule(schedule_id: int) -> dict:
        store = Store(load_config().db_path)
        job = store.get_schedule(schedule_id)
        if not job:
            store.close()
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="no such schedule")
        store.set_schedule_enabled(schedule_id, not job["enabled"])
        store.close()
        return {"ok": True, "enabled": not job["enabled"]}

    @app.delete("/schedules/{schedule_id}")
    async def delete_schedule(schedule_id: int) -> dict:
        store = Store(load_config().db_path)
        store.delete_schedule(schedule_id)
        store.close()
        return {"ok": True}

    # ── Documents / RAG ──────────────────────────────────────────────────────
    @app.get("/docs")
    async def list_docs() -> dict:
        store = Store(load_config().db_path)
        out = store.list_documents()
        store.close()
        return {"documents": out}

    @app.post("/docs/ingest")
    async def ingest_docs(body: dict) -> dict:
        from xplogent.core.rag import ingest_path, ingest_text
        cfg = load_config()
        store = Store(cfg.db_path)
        provider = build_provider(cfg.embedding_model)
        mem_embedder = Embedder(provider)
        try:
            if body.get("content"):
                res = await ingest_text(store, mem_embedder, str(body["content"]),
                                        str(body.get("title", "pasted")))
            elif body.get("path"):
                res = await ingest_path(store, mem_embedder, str(body["path"]))
            else:
                res = {"ok": False, "error": "provide 'path' or 'content'"}
        except Exception as exc:  # noqa: BLE001
            res = {"ok": False, "error": str(exc)}
        finally:
            await provider.aclose()
            store.close()
        return res

    @app.get("/docs/search")
    async def search_docs(q: str, k: int = 5) -> dict:
        from xplogent.core.rag import hybrid_search
        cfg = load_config()
        store = Store(cfg.db_path)
        provider = build_provider(cfg.embedding_model)
        try:
            hits = await hybrid_search(store, Embedder(provider), q, k=k)
        finally:
            await provider.aclose()
            store.close()
        return {"hits": hits}

    @app.delete("/docs/{doc_id}")
    async def delete_doc(doc_id: int) -> dict:
        store = Store(load_config().db_path)
        store.delete_document(doc_id)
        store.close()
        return {"ok": True}

    # ── Analytics (usage aggregation) ────────────────────────────────────────
    @app.get("/analytics")
    async def analytics(days: int = 30) -> dict:
        import time as _t
        store = Store(load_config().db_path)
        since = _t.time() - days * 86400
        rows = store.usage_rows(since)
        store.close()

        by_day: dict[str, dict] = {}
        by_model: dict[str, dict] = {}
        totals = {"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "turns": len(rows)}
        for r in rows:
            day = _t.strftime("%Y-%m-%d", _t.localtime(r["created_at"]))
            d = by_day.setdefault(day, {"day": day, "input_tokens": 0, "output_tokens": 0, "cost": 0.0, "turns": 0})
            m = by_model.setdefault(r["model"], {"model": r["model"], "input_tokens": 0, "output_tokens": 0, "cost": 0.0, "turns": 0})
            for bucket in (d, m, totals):
                bucket["input_tokens"] += r["input_tokens"] or 0
                bucket["output_tokens"] += r["output_tokens"] or 0
                bucket["cost"] += r["cost"] or 0.0
            d["turns"] += 1
            m["turns"] += 1
        totals["cost"] = round(totals["cost"], 4)
        return {
            "totals": totals,
            "by_day": sorted(by_day.values(), key=lambda x: x["day"]),
            "by_model": sorted(by_model.values(), key=lambda x: -x["turns"]),
        }

    # ── Audit log ────────────────────────────────────────────────────────────
    @app.get("/audit")
    async def audit_log(limit: int = 200, action: str = "", risk: str = "") -> dict:
        store = Store(load_config().db_path)
        rows = store.audit_rows(limit=limit, action=action, risk=risk)
        store.close()
        return {"entries": rows}

    # ── Knowledge graph ──────────────────────────────────────────────────────
    @app.get("/graph")
    async def graph(limit: int = 300) -> dict:
        store = Store(load_config().db_path)
        snap = store.graph_snapshot(limit=limit)
        store.close()
        return snap

    @app.get("/graph/neighbors")
    async def graph_neighbors(entity: str) -> dict:
        store = Store(load_config().db_path)
        rows = store.neighbors(entity, limit=50)
        store.close()
        return {"entity": entity, "edges": rows}

    # ── Visual workflows ─────────────────────────────────────────────────────
    @app.get("/workflows")
    async def list_workflows() -> dict:
        store = Store(load_config().db_path)
        out = store.list_workflows()
        store.close()
        return {"workflows": out}

    @app.post("/workflows")
    async def save_workflow(body: dict) -> dict:
        store = Store(load_config().db_path)
        wid = store.save_workflow(str(body.get("name", "workflow")),
                                  body.get("graph", {}), body.get("id"))
        out = store.get_workflow(wid)
        store.close()
        return {"ok": True, "workflow": out}

    @app.delete("/workflows/{workflow_id}")
    async def delete_workflow(workflow_id: int) -> dict:
        store = Store(load_config().db_path)
        store.delete_workflow(workflow_id)
        store.close()
        return {"ok": True}

    @app.post("/workflows/{workflow_id}/run")
    async def run_workflow_ep(workflow_id: int) -> dict:
        from xplogent.core.workflow import run_workflow

        store = Store(load_config().db_path)
        wf = store.get_workflow(workflow_id)
        store.close()
        if not wf:
            return {"ok": False, "error": "workflow not found"}
        _audit_event("workflow_run", target=wf.get("name", str(workflow_id)))
        return await run_workflow(wf["graph"])

    # ── Budget / cost guardrails ─────────────────────────────────────────────
    @app.get("/budget")
    async def get_budget() -> dict:
        from xplogent.core.budget import today_spend

        cfg = load_config()
        store = Store(cfg.db_path)
        spent = today_spend(store)
        store.close()
        return {"budget": cfg.budget, "today_spend": spent}

    @app.post("/budget")
    async def set_budget(body: dict) -> dict:
        merged = save_user_config({"budget": body})
        _audit_event("budget_change", target=",".join(str(k) for k in body.keys()))
        return {"ok": True, "budget": merged.get("budget", {})}

    # ── Proactive triggers (webhook + file-watch) ────────────────────────────
    @app.get("/triggers")
    async def list_triggers() -> dict:
        store = Store(load_config().db_path)
        out = store.list_triggers()
        store.close()
        return {"triggers": out}

    @app.post("/triggers")
    async def create_trigger(body: dict) -> dict:
        ttype = str(body.get("type", "webhook"))
        # Webhook triggers get a random secret token as their spec.
        spec = str(body.get("spec", ""))
        if ttype == "webhook" and not spec:
            spec = uuid.uuid4().hex
        store = Store(load_config().db_path)
        tid = store.add_trigger(str(body.get("name", "trigger")), ttype, spec,
                                str(body.get("prompt", "")), str(body.get("mode", "agent")))
        out = store.get_trigger(tid)
        store.close()
        _audit_event("trigger_create", target=str(body.get("name", "")), risk="medium")
        return {"ok": True, "trigger": out}

    @app.post("/triggers/{trigger_id}/toggle")
    async def toggle_trigger(trigger_id: int) -> dict:
        store = Store(load_config().db_path)
        store.toggle_trigger(trigger_id)
        store.close()
        return {"ok": True}

    @app.delete("/triggers/{trigger_id}")
    async def delete_trigger(trigger_id: int) -> dict:
        store = Store(load_config().db_path)
        store.delete_trigger(trigger_id)
        store.close()
        return {"ok": True}

    @app.post("/triggers/webhook/{token}")
    async def fire_webhook(token: str, request: Request) -> dict:
        cfg = load_config()
        store = Store(cfg.db_path)
        trig = store.get_trigger_by_token(token)
        store.close()
        if not trig or not trig.get("enabled"):
            return JSONResponse({"error": "unknown or disabled webhook"}, status_code=404)
        try:
            body = (await request.body()).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
        from xplogent.core.triggers import run_trigger

        # Fire-and-forget so the caller gets a fast 202.
        asyncio.create_task(run_trigger(trig, context=body, config=cfg))
        return {"ok": True, "fired": trig["name"]}

    # ── Evals ────────────────────────────────────────────────────────────────
    @app.get("/evals")
    async def list_evals() -> dict:
        store = Store(load_config().db_path)
        out = store.list_evals()
        store.close()
        return {"evals": out}

    @app.post("/evals")
    async def save_eval(body: dict) -> dict:
        store = Store(load_config().db_path)
        eid = store.upsert_eval(str(body.get("name", "suite")), str(body.get("description", "")))
        if isinstance(body.get("cases"), list):
            store.set_eval_cases(eid, body["cases"])
        out = next((e for e in store.list_evals() if e["id"] == eid), None)
        store.close()
        return {"ok": True, "eval": out}

    @app.delete("/evals/{eval_id}")
    async def delete_eval(eval_id: int) -> dict:
        store = Store(load_config().db_path)
        store.delete_eval(eval_id)
        store.close()
        return {"ok": True}

    @app.post("/evals/{eval_id}/run")
    async def run_eval(eval_id: int) -> dict:
        from xplogent.core.evals import run_suite
        try:
            return await run_suite(eval_id)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    @app.post("/evals/{eval_id}/ab")
    async def run_eval_ab(eval_id: int, body: dict) -> dict:
        """A/B-test a suite across prompt/model variants and return a winner."""
        from xplogent.core.evals import run_ab
        variants = body.get("variants") or []
        if len(variants) < 2:
            return {"ok": False, "error": "provide at least two variants"}
        try:
            res = await run_ab(eval_id, variants)
            return {"ok": True, **res}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    @app.post("/evals/promote")
    async def promote_eval(body: dict) -> dict:
        """Promote a winning variant's prompt/model to the live config."""
        from xplogent.core.evals import promote_variant
        out = promote_variant(str(body.get("system_prompt", "")), str(body.get("model", "")))
        _audit_event("eval_promote", target=",".join(out.get("promoted", [])), risk="medium")
        return out

    # ── Persona (SOUL.md) + curated memory (MEMORY.md) ───────────────────────
    @app.get("/persona/soul")
    async def get_soul() -> dict:
        from xplogent.core.persona import load_soul
        return {"content": load_soul()}

    @app.put("/persona/soul")
    async def put_soul(body: TextBody) -> dict:
        from xplogent.core.persona import save_soul
        save_soul(body.content)
        return {"ok": True}

    @app.get("/persona/memory")
    async def get_memory_md() -> dict:
        from xplogent.core.persona import load_memory
        return {"content": load_memory()}

    @app.put("/persona/memory")
    async def put_memory_md(body: TextBody) -> dict:
        from xplogent.core.persona import save_memory
        save_memory(body.content)
        return {"ok": True}

    @app.post("/memory/compact")
    async def memory_compact() -> dict:
        from xplogent.core.persona import compact_memory
        cfg = load_config()
        store = Store(cfg.db_path)
        provider = build_provider(cfg.reflection_model)
        try:
            content = await compact_memory(store, provider)
        finally:
            await provider.aclose()
            store.close()
        return {"ok": True, "content": content}

    @app.get("/skills/history")
    async def skills_history(limit: int = 50) -> dict:
        store = Store(load_config().db_path)
        out = store.skill_events(limit=limit)
        store.close()
        return {"events": out}

    @app.post("/memory/consolidate")
    async def memory_consolidate() -> dict:
        """End-of-conversation save: flush every skill to the skills folder and
        distill MEMORY.md. Reused by the GUI when a chat ends."""
        from xplogent.core.persona import compact_memory
        from xplogent.skills.manager import SkillManager

        cfg = load_config()
        store = Store(cfg.db_path)
        embed_provider = build_provider(cfg.embedding_model)
        provider = build_provider(cfg.reflection_model)
        try:
            sm = SkillManager(MemoryManager(store, Embedder(embed_provider)), cfg.skills_dir)
            exported = await asyncio.to_thread(sm.export_all)
            content = await compact_memory(store, provider)
        finally:
            await embed_provider.aclose()
            await provider.aclose()
            store.close()
        return {"ok": True, "skills_saved": exported, "memory": content}

    # ── Backup / restore + knowledge export/import ───────────────────────────
    @app.get("/backup")
    async def backup_download(include_secrets: bool = False):
        from fastapi.responses import FileResponse

        from xplogent.core import backup as backup_mod
        res = await asyncio.to_thread(backup_mod.create_backup, None, include_secrets=include_secrets)
        return FileResponse(res["path"], filename="xplogent-backup.tar.gz",
                            media_type="application/gzip")

    @app.post("/restore")
    async def backup_restore(request: Request) -> dict:
        """Restore from an uploaded .tar.gz sent as the raw request body."""
        import tempfile

        from xplogent.core import backup as backup_mod
        data = await request.body()
        if not data:
            return {"ok": False, "error": "empty upload"}
        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        return await asyncio.to_thread(backup_mod.restore_backup, tmp_path)

    @app.get("/export/knowledge")
    async def export_knowledge() -> dict:
        from xplogent.core.backup import export_knowledge as _export
        store = Store(load_config().db_path)
        try:
            return _export(store)
        finally:
            store.close()

    @app.post("/import/knowledge")
    async def import_knowledge(body: dict) -> dict:
        from xplogent.core.backup import import_knowledge as _import
        store = Store(load_config().db_path)
        try:
            return _import(store, body)
        finally:
            store.close()

    # ── One-click update ─────────────────────────────────────────────────────
    @app.get("/update/check")
    async def update_check() -> dict:
        return await asyncio.to_thread(updater.check_update)

    @app.post("/update")
    async def update_apply() -> dict:
        # Safety net: snapshot data before changing code.
        try:
            from xplogent.core.backup import create_backup
            backed_up = (await asyncio.to_thread(create_backup)).get("path")
        except Exception:  # noqa: BLE001
            backed_up = None
        pulled = await asyncio.to_thread(updater.pull)
        if not pulled["ok"]:
            return {"ok": False, "stage": "pull", "output": pulled["output"], "backup": backed_up}
        installed = await asyncio.to_thread(updater.reinstall)
        web = await asyncio.to_thread(updater.rebuild_web)  # so GUI changes deploy
        # Re-exec shortly after this response flushes so new code loads.
        asyncio.get_event_loop().call_later(0.5, lambda: updater.restart(_serve_args))
        return {"ok": True, "restarting": True, "backup": backed_up, "pull": pulled["output"],
                "install": installed["output"], "web": web.get("output") or web.get("skipped")}

    # ── In-app guide ─────────────────────────────────────────────────────────
    @app.get("/guide")
    async def guide_list() -> dict:
        return {"pages": guide.list_pages()}

    @app.get("/guide/{slug}")
    async def guide_page(slug: str) -> dict:
        content = guide.read_page(slug)
        if content is None:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="no such page")
        return {"slug": slug, "content": content}

    # ── Serve the built dashboard (same origin) ──────────────────────────────
    _mount_dashboard(app)
    return app


# Args used when the server re-execs itself after an update.
_serve_args = ["up"]


def _dashboard_dir():
    from pathlib import Path

    candidates = []
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
    global _serve_args
    _serve_args = ["up", "--port", str(port)]

    # Security: when exposed beyond localhost, require an access token. Generate
    # and persist one automatically if the operator hasn't set their own.
    _is_local = host in ("127.0.0.1", "localhost", "::1", "")
    if not _is_local:
        cfg = load_config()
        if not str(cfg.raw.get("server", {}).get("auth_token") or "").strip():
            import secrets as _pysecrets

            token = _pysecrets.token_urlsafe(32)
            save_user_config({"server": {"auth_token": token}})
            print("\n  Binding to a non-local host — access token enabled.")
            print(f"  Your dashboard token:  {token}")
            print("  Paste it when the dashboard prompts (Settings → re-generate to rotate).\n")

    if open_browser:
        import threading
        import webbrowser

        url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}"
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
        if _dashboard_dir() is None:
            print("Dashboard not built yet — serving API only. "
                  "Build it with: cd web && npm install && npm run build")

    uvicorn.run(create_app(), host=host, port=port)
