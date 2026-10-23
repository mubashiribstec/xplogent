"""Settings: full config, edits, secrets, tools, roles."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from xplogent.core.config import load_config, save_env, save_user_config, secret_status
from xplogent.interfaces.api import deps
from xplogent.interfaces.api.models import (
    ConfigPatch,
    RolePatch,
    SecretsPatch,
)
from xplogent.providers.registry import available_providers
from xplogent.tools.registry import _BUILTIN_GROUPS

router = APIRouter()


# ── Settings: full config, edits, secrets, tools, roles ──────────────────


@router.get("/config/full")
async def config_full() -> dict:
    cfg = load_config()
    return {
        "model": cfg.model,
        "reflection_model": cfg.reflection_model,
        "embedding_model": cfg.embedding_model,
        "vision_model": cfg.vision_model,
        "memory": cfg.memory,
        "safety": cfg.safety,
        "execution": cfg.execution,
        "roles": cfg.roles,
        "tools_enabled": cfg.tools.get("enabled", []),
        "models": cfg.models,
        "providers": available_providers(),
        "secrets": secret_status(),
        # Read back by the Calls view and the Ollama card (they used to show blank fields).
        "voip": cfg.voip,
        "ollama": {"host": str((cfg.raw.get("ollama") or {}).get("host") or "")},
    }


@router.patch("/config")
async def patch_config(body: ConfigPatch) -> dict:
    merged = save_user_config(body.updates)
    deps.audit_event("config_change", target=",".join(body.updates.keys())[:200])
    return {"ok": True, "config": merged}


@router.put("/secrets")
async def put_secrets(body: SecretsPatch) -> dict:
    try:
        save_env(body.keys)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    deps.audit_event("secret_change", target=",".join(body.keys.keys()), risk="high")
    return {"ok": True, "secrets": secret_status()}


@router.get("/tools")
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


@router.get("/roles")
async def roles() -> dict:
    return {"roles": load_config().roles}


@router.put("/roles/{name}")
async def put_role(name: str, body: RolePatch) -> dict:
    merged = save_user_config({"roles": {name: body.model_dump()}})
    return {"ok": True, "roles": merged.get("roles", {})}
