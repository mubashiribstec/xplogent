"""Health, status, providers, vision, analytics, audit, budget, backup, update, guide."""

from __future__ import annotations

import asyncio
import os

from fastapi import APIRouter, Request

from xplogent.core import guide, updater
from xplogent.core.config import load_config, save_env, save_user_config, secret_status
from xplogent.core.events import EventBus
from xplogent.interfaces.api import deps
from xplogent.interfaces.api.models import (
    RunRequest,
)
from xplogent.interfaces.api.security import token_matches
from xplogent.memory.store import Store
from xplogent.providers.registry import available_providers

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    cfg = load_config()
    return {"status": "ok", "model": cfg.model, "providers": available_providers(),
            "auth": bool(deps.auth_token())}


@router.get("/auth/check")
async def auth_check(request: Request) -> dict:
    """Public: tells the dashboard whether auth is on and whether a token is valid."""
    token = deps.auth_token()
    if not token:
        return {"required": False, "ok": True}
    ok = token_matches(deps.request_token(request), token)
    return {"required": True, "ok": ok}


@router.get("/status")
async def status() -> dict:
    """Aggregate health for the dashboard: providers, which keys are set, Ollama up."""

    from xplogent.core.platform import has_display
    from xplogent.providers.ollama import ollama_settings

    cfg = load_config()
    ollama_host, token = ollama_settings()
    probe = await _probe_ollama(ollama_host, token, timeout=3.0)
    return {
        "status": "ok",
        "model": cfg.model,
        "providers": available_providers(),
        "secrets": secret_status(),
        "ollama": {"host": ollama_host, "reachable": probe["ok"], "auth": bool(token),
                   "error": probe.get("error", "")},
        # What this install can do, so the dashboard can disable what can't work here.
        "capabilities": {"display": has_display(), "docker": updater.in_docker(),
                         "git": updater.repo_root() is not None and not updater.in_docker()},
    }


async def _probe_ollama(host: str, token: str, timeout: float = 8.0) -> dict:
    """GET /api/tags on an Ollama server: ``{ok, models, error}`` (never raises)."""
    import httpx

    from xplogent.providers.ollama import ollama_headers

    try:
        async with httpx.AsyncClient(timeout=timeout, headers=ollama_headers(token),
                                     follow_redirects=True) as client:
            resp = await client.get(f"{host.rstrip('/')}/api/tags")
    except httpx.HTTPError as exc:
        return {"ok": False, "models": [], "error": f"cannot reach {host}: {type(exc).__name__}"}
    if resp.status_code in (401, 403):
        return {"ok": False, "models": [],
                "error": "the server refused the token (401/403): check the bearer token"}
    if resp.status_code != 200:
        return {"ok": False, "models": [], "error": f"{host} answered HTTP {resp.status_code}"}
    try:
        models = sorted(m.get("name", "") for m in resp.json().get("models", []) if m.get("name"))
    except ValueError:
        return {"ok": False, "models": [],
                "error": "that address answered, but not like an Ollama server (/api/tags)"}
    return {"ok": True, "models": models}


@router.post("/providers/ollama/test")
async def ollama_test(body: dict) -> dict:
    """Check an Ollama URL + bearer token before saving them (Settings → Ollama server).

    Falls back to the saved values for anything not given, so the token never has to be
    sent back to the browser.
    """
    from xplogent.providers.ollama import ollama_settings

    saved_host, saved_token = ollama_settings()
    host = str(body.get("host") or saved_host).strip()
    token = str(body.get("token") or saved_token).strip()
    if not host.startswith(("http://", "https://")):
        return {"ok": False, "models": [], "error": "the URL must start with http:// or https://"}
    return await _probe_ollama(host, token)


@router.post("/providers/ollama/settings")
async def ollama_save(body: dict) -> dict:
    """Save the Ollama server URL (config) and bearer token (encrypted store)."""
    host = str(body.get("host", "")).strip().rstrip("/")
    if host and not host.startswith(("http://", "https://")):
        return {"ok": False, "error": "the URL must start with http:// or https://"}
    save_user_config({"ollama": {"host": host}})
    token = str(body.get("token") or "").strip()
    if token:
        try:
            save_env({"OLLAMA_API_KEY": token})
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
    deps.audit_event("ollama_settings", target=host, risk="medium")
    return {"ok": True, "host": host}


# ── Backup model (local Ollama in Docker) ─────────────────────────────────────
@router.get("/providers/backup")
async def backup_model_status() -> dict:
    from xplogent.providers.ollama import local_ollama_host

    fb = load_config().raw.get("fallback") or {}
    host = local_ollama_host()
    probe = await _probe_ollama(host, "", timeout=4.0)
    model = str(fb.get("model") or "llama3.2:3b")
    installed = any(m == model or m == f"{model}:latest" or m.split(":")[0] == model
                    for m in probe["models"])
    return {"enabled": bool(fb.get("enabled")), "model": model, "host": host,
            "reachable": probe["ok"], "models": probe["models"], "installed": installed,
            "error": probe.get("error", "")}


@router.post("/providers/backup")
async def backup_model_save(body: dict) -> dict:
    updates: dict = {}
    if "enabled" in body:
        updates["enabled"] = bool(body["enabled"])
    if str(body.get("model") or "").strip():
        updates["model"] = str(body["model"]).strip()
    if "host" in body:
        host = str(body.get("host") or "").strip().rstrip("/")
        if host and not host.startswith(("http://", "https://")):
            return {"ok": False, "error": "the URL must start with http:// or https://"}
        updates["host"] = host
    save_user_config({"fallback": updates})
    deps.audit_event("backup_model", target=str(updates), risk="low")
    return {"ok": True, **(load_config().raw.get("fallback") or {})}


@router.post("/providers/backup/pull")
async def backup_model_pull(body: dict) -> dict:
    """Download the backup model onto the local Ollama (can take several minutes)."""
    import httpx

    from xplogent.providers.ollama import local_ollama_host

    model = str(body.get("model") or (load_config().raw.get("fallback") or {}).get("model")
                or "llama3.2:3b").strip()
    host = local_ollama_host()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(3600.0, connect=10.0)) as client:
            resp = await client.post(f"{host}/api/pull", json={"model": model, "stream": False})
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"local Ollama not reachable at {host} ({type(exc).__name__}): "
                                      "on the server run `xplogent-docker backup-model on`"}
    if resp.status_code != 200:
        return {"ok": False, "error": f"pull failed: HTTP {resp.status_code} {resp.text[:300]}"}
    return {"ok": True, "model": model}


@router.post("/providers/backup/test")
async def backup_model_test() -> dict:
    """One short reply straight from the backup model (bypasses the main model)."""
    import time

    from xplogent.providers.base import Message, Role
    from xplogent.providers.registry import build_provider

    fb = load_config().raw.get("fallback") or {}
    model = str(fb.get("model") or "llama3.2:3b")
    spec = model if ":" in model and model.split(":", 1)[0] in available_providers() \
        else f"ollama-local:{model}"
    provider = build_provider(spec, fallback=False)
    started = time.monotonic()
    try:
        reply = await asyncio.wait_for(provider.complete(
            [Message(role=Role.USER, content="Reply with one short friendly sentence.")],
            temperature=0.2), timeout=180)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
    finally:
        await provider.aclose()
    return {"ok": bool(reply.content.strip()), "reply": reply.content.strip()[:300],
            "seconds": round(time.monotonic() - started, 1), "model": spec}


@router.post("/providers/ollama/pull")
async def ollama_pull(body: dict) -> dict:
    """Pull a local Ollama model (used by onboarding). Best-effort, blocking."""
    import httpx

    from xplogent.providers.ollama import ollama_headers, ollama_settings

    model = str(body.get("model", "")).strip()
    if not model:
        return {"ok": False, "error": "no model given"}
    # Through the configured server's API (works for a remote/authenticated Ollama and
    # inside Docker, where there is no `ollama` CLI).
    host, token = ollama_settings()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(1800.0, connect=10.0),
                                     headers=ollama_headers(token)) as client:
            resp = await client.post(f"{host}/api/pull", json={"model": model, "stream": False})
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"cannot reach {host}: {type(exc).__name__}"}
    if resp.status_code != 200:
        return {"ok": False, "error": f"pull failed: HTTP {resp.status_code} {resp.text[:300]}"}
    return {"ok": True, "output": resp.text[-2000:]}


@router.get("/providers/{name}/models")
async def provider_models(name: str) -> dict:
    """Fetch a provider's live model catalog (using its configured key/host)."""
    if name not in available_providers():
        return {"models": [], "error": f"unknown provider '{name}'"}
    try:
        prov = deps.build_provider(f"{name}:_")
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


@router.post("/vision/enable-local")
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


@router.post("/vision/test")
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
    try:
        res = await AnalyzeImageTool().run(path=path, question="What color is this image?")
    finally:
        os.unlink(path)
    return {"ok": res.ok, "model": model,
            "reply": res.output if res.ok else res.error}


@router.get("/config")
async def config() -> dict:
    cfg = load_config()
    return {
        "model": cfg.model,
        "reflection_model": cfg.reflection_model,
        "embedding_model": cfg.embedding_model,
        "tools_enabled": cfg.tools.get("enabled", []),
        "providers": available_providers(),
    }


@router.get("/skills")
async def skills() -> dict:
    cfg = load_config()
    with Store(cfg.db_path) as store:
        out = [
            {"name": s.name, "description": s.description, "uses": s.uses,
             "level": s.level, "stars": s.stars, "successes": s.successes,
             "failures": s.failures, "trigger": s.trigger, "source": s.source}
            for s in store.all_skills()
        ]
    return {"skills": out}


@router.get("/memory/search")
async def memory_search(q: str) -> dict:
    cfg = load_config()
    with Store(cfg.db_path) as store:
        facts = [f.content for f in store.all_facts() if q.lower() in f.content.lower()]
        msgs = store.search_messages(q, limit=10)
    return {"facts": facts, "messages": msgs}


@router.post("/run")
async def run(req: RunRequest) -> dict:
    """One-shot run. Confirmation-tier tools are blocked (no interactive approver)."""
    bus = EventBus()
    runtime = deps.build_runtime(bus=bus)
    answer = await runtime.agent.run(req.task)
    await runtime.aclose()
    return {"answer": answer}


# ── Model presets ────────────────────────────────────────────────────────


@router.get("/models")
async def models() -> dict:
    cfg = load_config()
    return {"models": cfg.models, "active": cfg.model, "providers": available_providers()}


# ── Analytics (usage aggregation) ────────────────────────────────────────


@router.get("/analytics")
async def analytics(days: int = 30) -> dict:
    import time as _t
    with Store(load_config().db_path) as store:
        since = _t.time() - days * 86400
        rows = store.usage_rows(since)

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


@router.get("/audit")
async def audit_log(limit: int = 200, action: str = "", risk: str = "") -> dict:
    with Store(load_config().db_path) as store:
        rows = store.audit_rows(limit=limit, action=action, risk=risk)
    return {"entries": rows}


# ── Budget / cost guardrails ─────────────────────────────────────────────


@router.get("/budget")
async def get_budget() -> dict:
    from xplogent.core.budget import today_spend

    cfg = load_config()
    with Store(cfg.db_path) as store:
        spent = today_spend(store)
    return {"budget": cfg.budget, "today_spend": spent}


@router.post("/budget")
async def set_budget(body: dict) -> dict:
    merged = save_user_config({"budget": body})
    deps.audit_event("budget_change", target=",".join(str(k) for k in body.keys()))
    return {"ok": True, "budget": merged.get("budget", {})}


# ── Backup / restore + knowledge export/import ───────────────────────────


@router.get("/backup")
async def backup_download(include_secrets: bool = False):
    from fastapi.responses import FileResponse

    from xplogent.core import backup as backup_mod
    res = await asyncio.to_thread(backup_mod.create_backup, None, include_secrets=include_secrets)
    return FileResponse(res["path"], filename="xplogent-backup.tar.gz",
                        media_type="application/gzip")


@router.post("/restore")
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
    try:
        return await asyncio.to_thread(backup_mod.restore_backup, tmp_path)
    finally:
        os.unlink(tmp_path)  # may hold API keys when the backup included secrets


@router.get("/export/knowledge")
async def export_knowledge() -> dict:
    from xplogent.core.backup import export_knowledge as _export
    store = Store(load_config().db_path)
    try:
        return _export(store)
    finally:
        store.close()


@router.post("/import/knowledge")
async def import_knowledge(body: dict) -> dict:
    from xplogent.core.backup import import_knowledge as _import
    store = Store(load_config().db_path)
    try:
        return _import(store, body)
    finally:
        store.close()


# ── One-click update ─────────────────────────────────────────────────────


@router.get("/update/check")
async def update_check() -> dict:
    return await asyncio.to_thread(updater.check_update)


@router.post("/update")
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
    asyncio.get_event_loop().call_later(0.5, lambda: updater.restart(deps.SERVE_ARGS))
    return {"ok": True, "restarting": True, "backup": backed_up, "pull": pulled["output"],
            "install": installed["output"], "web": web.get("output") or web.get("skipped")}


# ── In-app guide ─────────────────────────────────────────────────────────


@router.get("/guide")
async def guide_list() -> dict:
    return {"pages": guide.list_pages()}


@router.get("/guide/{slug}")
async def guide_page(slug: str) -> dict:
    content = guide.read_page(slug)
    if content is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="no such page")
    return {"slug": slug, "content": content}
