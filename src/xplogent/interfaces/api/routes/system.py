"""Health, status, providers, analytics, audit, budget, backup, guide."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re

from fastapi import APIRouter, Request

from xplogent.core import guide, repo_info
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
            "auth": bool(deps.auth_token()), **repo_info.build_info()}


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
        **repo_info.build_info(),
        "model": cfg.model,
        "providers": available_providers(),
        "secrets": secret_status(),
        "ollama": {"host": ollama_host, "reachable": probe["ok"], "auth": bool(token),
                   "error": probe.get("error", "")},
        # What this install can do, so the dashboard can disable what can't work here.
        "capabilities": {"display": has_display(), "docker": repo_info.in_docker(),
                         "git": repo_info.repo_root() is not None and not repo_info.in_docker()},
    }


async def _probe_ollama(host: str, token: str, timeout: float = 8.0) -> dict:
    """GET /api/tags on an Ollama server: ``{ok, models, error}`` (never raises)."""
    import httpx

    from xplogent.providers.ollama import ollama_headers

    try:
        async with httpx.AsyncClient(timeout=timeout, headers=ollama_headers(token),
                                     follow_redirects=True) as client:
            resp = await client.get(f"{host.rstrip('/')}/api/tags")
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        # InvalidURL is not an HTTPError in current httpx: a malformed saved host
        # (bad port, stray characters) must read as "fix the address", not a 500.
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
    from xplogent.providers.ollama import ollama_server, ollama_settings, server_token_env

    server = str(body.get("server") or "").strip()
    if server and not body.get("host"):
        try:
            saved_host, saved_token = ollama_server(server)
        except ValueError as exc:
            return {"ok": False, "models": [], "error": str(exc)}
    elif server:
        saved_host, saved_token = "", os.environ.get(server_token_env(server), "")
    else:
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


_SERVER_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")


@router.get("/providers/ollama/servers")
async def ollama_servers_list() -> dict:
    """Named Ollama servers (Settings → Ollama servers); tokens are never returned."""
    from xplogent.providers.ollama import ollama_servers, server_token_env

    return {"servers": [{**s, "token_set": bool(os.environ.get(server_token_env(s["name"])))}
                        for s in ollama_servers()]}


@router.post("/providers/ollama/servers")
async def ollama_servers_save(body: dict) -> dict:
    """Add or update a named server ``{name, host, token?}`` (token → encrypted store)."""
    from xplogent.providers.ollama import ollama_servers, server_token_env

    name = str(body.get("name") or "").strip().lower()
    host = str(body.get("host") or "").strip().rstrip("/")
    if not _SERVER_NAME.fullmatch(name) or name == "default":
        return {"ok": False, "error": "the name must be letters, digits or dashes (e.g. gpu-box), not 'default'"}
    if not host.startswith(("http://", "https://")):
        return {"ok": False, "error": "the URL must start with http:// or https://"}
    servers = [s for s in ollama_servers() if s["name"] != name] + [{"name": name, "host": host}]
    _save_servers(servers)
    token = str(body.get("token") or "").strip()
    if token:
        try:
            save_env({server_token_env(name): token})
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
    deps.audit_event("ollama_server_saved", target=f"{name} {host}", risk="medium")
    return {"ok": True, **(await ollama_servers_list())}


@router.delete("/providers/ollama/servers/{name}")
async def ollama_servers_delete(name: str) -> dict:
    from xplogent.core.config import delete_env
    from xplogent.providers.ollama import ollama_servers, server_token_env

    _save_servers([s for s in ollama_servers() if s["name"] != name])
    delete_env([server_token_env(name)])
    deps.audit_event("ollama_server_removed", target=name, risk="medium")
    return {"ok": True, **(await ollama_servers_list())}


def _save_servers(servers: list[dict]) -> None:
    """The servers list replaces the stored one (a deep merge can't remove entries)."""
    from xplogent.core.config import xplogent_home

    path = xplogent_home() / "config.yaml"
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    data = data if isinstance(data, dict) else {}
    ollama = data.get("ollama") if isinstance(data.get("ollama"), dict) else {}
    ollama["servers"] = servers
    data["ollama"] = ollama
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _backup_target() -> tuple[str, str, str, str]:
    """``(spec, host, token, server)`` of the backup model: the local Docker Ollama, or a
    named server (config ``fallback.server``), or any other provider spec."""
    from xplogent.providers.ollama import local_ollama_host, ollama_server
    from xplogent.providers.registry import backup_target_spec

    fb = load_config().raw.get("fallback") or {}
    server = str(fb.get("server") or "").strip()
    spec = backup_target_spec(fb)
    if server:
        try:
            host, token = ollama_server(server)
        except ValueError:
            host, token = "", ""
        return spec, host, token, server
    return spec, local_ollama_host(), "", ""


# ── Backup model (local Ollama in Docker) ─────────────────────────────────────
@router.get("/providers/backup")
async def backup_model_status() -> dict:
    fb = load_config().raw.get("fallback") or {}
    _spec, host, token, server = _backup_target()
    probe = await _probe_ollama(host, token, timeout=4.0) if host else {
        "ok": False, "models": [], "error": f"no Ollama server named '{server}'"}
    model = str(fb.get("model") or "llama3.2:3b")
    installed = any(m == model or m == f"{model}:latest" or m.split(":")[0] == model
                    for m in probe["models"])
    return {"enabled": bool(fb.get("enabled")), "model": model, "host": host, "server": server,
            "reachable": probe["ok"], "models": probe["models"], "installed": installed,
            "error": probe.get("error", "")}


@router.post("/providers/backup")
async def backup_model_save(body: dict) -> dict:
    updates: dict = {}
    if "enabled" in body:
        updates["enabled"] = bool(body["enabled"])
    if str(body.get("model") or "").strip():
        updates["model"] = str(body["model"]).strip()
    if "server" in body:
        updates["server"] = str(body.get("server") or "").strip()
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

    from xplogent.providers.ollama import ollama_headers

    model = str(body.get("model") or (load_config().raw.get("fallback") or {}).get("model")
                or "llama3.2:3b").strip()
    _spec, host, token, _server = _backup_target()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(3600.0, connect=10.0),
                                     headers=ollama_headers(token)) as client:
            resp = await client.post(f"{host}/api/pull", json={"model": model, "stream": False})
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
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

    spec = _backup_target()[0]
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


_TEST_TOOL = {"name": "book_callback", "description": "Book a callback for the caller at the time they ask for.",
              "parameters": {"type": "object", "properties": {"when": {"type": "string"}}, "required": ["when"]}}


@router.post("/providers/test-model")
async def test_model(body: dict) -> dict:
    """One request the way a call sends it (system prompt + a tool), timed, with the
    real reason and the fix when it fails. ``spec`` defaults to the call model.
    For OpenRouter it also reports the key's free-model requests left today."""
    import time

    from xplogent.providers.base import Message, Role, StreamKind, ToolSpec
    from xplogent.providers.diagnose import diagnose
    from xplogent.providers.registry import build_provider
    from xplogent.voip.readiness import _PROVIDER_KEYS

    cfg = load_config()
    spec = str(body.get("spec") or "").strip() or str((cfg.voip or {}).get("model") or cfg.model)
    kind = spec.split(":", 1)[0]
    out: dict = {"spec": spec, "ok": False}
    env = _PROVIDER_KEYS.get(kind)
    if env and not os.environ.get(env, "").strip():
        return {**out, "code": "no_key", "reason": f"No {env} is saved on this server.",
                "fix": f"Paste your key in Settings → API keys → {env}."}
    quota = None
    try:
        provider = build_provider(spec, fallback=False)
    except Exception as exc:  # noqa: BLE001 - e.g. unknown Ollama server
        return {**out, **diagnose(exc, spec)}
    try:
        if kind == "vercel" and provider.model == "auto":
            # "Test key" with no model chosen yet: try a quick one the key can use.
            from xplogent.providers.vercel import pick_quick_model

            try:
                picked = pick_quick_model(await asyncio.wait_for(provider.model_entries(), timeout=15))
            except Exception as exc:  # noqa: BLE001 - a refused key shows up here first
                return {**out, **diagnose(exc, spec)}
            if not picked:
                return {**out, "code": "no_models", "reason": "The gateway lists no chat models for this key.",
                        "fix": "Check the key's team and its AI Gateway settings on vercel.com."}
            provider.model = picked
            spec = out["spec"] = f"vercel:{picked}"
        if kind == "openrouter" and hasattr(provider, "key_info"):
            try:
                quota = await asyncio.wait_for(provider.key_info(), timeout=10)
                out["quota"] = quota
            except Exception as exc:  # noqa: BLE001 - the chat test below says more
                if getattr(getattr(exc, "response", None), "status_code", 0) == 401:
                    return {**out, **diagnose(exc, spec)}
        messages = [Message(role=Role.SYSTEM, content="You are a phone agent for a small company. Keep replies "
                            "short. Use book_callback when the caller asks to be called back."),
                    Message(role=Role.USER, content="Can someone call me back tomorrow at 10am?")]
        started = time.monotonic()
        first = 0.0
        text: list[str] = []
        final = None

        async def run() -> None:
            nonlocal first, final
            async for ev in provider.stream(messages, [ToolSpec(**_TEST_TOOL)], temperature=0.2):
                if ev.kind == StreamKind.TOKEN and ev.text:
                    first = first or time.monotonic()
                    text.append(ev.text)
                elif ev.kind == StreamKind.DONE:
                    final = ev.message

        await asyncio.wait_for(run(), timeout=float(body.get("timeout") or 60))
        done = time.monotonic()
        calls = [{"name": c.name, "arguments": c.arguments} for c in (getattr(final, "tool_calls", None) or [])]
        reply = ("".join(text) or getattr(final, "content", "") or "").strip()
        return {**out, "ok": bool(reply or calls), "reply": reply[:300], "tool_call": calls[0] if calls else None,
                "ms_first": round(((first or done) - started) * 1000), "ms_total": round((done - started) * 1000),
                **({} if reply or calls else {"code": "empty", "reason": "The model answered with nothing.",
                                               "fix": "Retry, or pick another model."})}
    except Exception as exc:  # noqa: BLE001 - explained to the user
        return {**out, **diagnose(exc, spec, quota)}
    finally:
        with contextlib.suppress(Exception):
            await provider.aclose()


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
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
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
    try:
        answer = await runtime.agent.run(req.task)
    finally:
        await runtime.aclose()  # provider clients + Store leak if run() raises
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
    deps.audit_event("backup_download",
                     target=f"include_secrets={str(include_secrets).lower()}")
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
    if len(data) > 1_000_000_000:
        return {"ok": False, "error": "upload too large (limit 1 GB)"}
    deps.audit_event("backup_restore", target=f"{len(data)} bytes uploaded")
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    try:
        result = await asyncio.to_thread(backup_mod.restore_backup, tmp_path)
        deps.audit_event("backup_restore_result", target=f"ok={result.get('ok')}")
        return result
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
