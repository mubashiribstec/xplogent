"""White-label-lite branding: company name, logo, accent colour; and dashboard UI state.

``GET /branding`` and ``GET /branding/logo`` are public (the login page shows them).
The logo must be a real PNG, JPEG or WebP (checked by its bytes, not its name), at most
512 KB. SVG is refused on purpose: it can carry scripts.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from xplogent.core.config import data_dir, load_config, save_user_config
from xplogent.interfaces.api import deps

router = APIRouter()

MAX_LOGO_BYTES = 512 * 1024
_TYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}
_HEX = re.compile(r"#[0-9a-fA-F]{6}")


def _logo_kind(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _logo_path():
    folder = data_dir() / "branding"
    for ext in _TYPES:
        path = folder / f"logo.{ext}"
        if path.is_file():
            return path
    return None


def _ui() -> dict:
    return dict(load_config().raw.get("ui") or {})


@router.get("/branding")
async def branding() -> dict:
    cfg = load_config()
    logo = _logo_path()
    company = str((cfg.voip or {}).get("company") or "")
    if company == "our company":                     # the shipped placeholder, not a brand
        company = ""
    return {"company": company, "accent": str(_ui().get("accent") or ""),
            "logo": logo is not None, "logo_version": int(logo.stat().st_mtime) if logo else 0}


@router.get("/branding/logo")
async def branding_logo():
    path = _logo_path()
    if path is None:
        return JSONResponse({"error": "no logo"}, status_code=404)
    return FileResponse(path, media_type=_TYPES[path.suffix[1:]],
                        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache"})


@router.post("/branding/logo")
async def branding_logo_upload(file: UploadFile) -> dict:
    data = await file.read(MAX_LOGO_BYTES + 1)
    if len(data) > MAX_LOGO_BYTES:
        return {"ok": False, "error": "the logo must be 512 KB or smaller"}
    kind = _logo_kind(data)
    if kind is None:
        return {"ok": False, "error": "the logo must be a PNG, JPEG or WebP image (SVG is not accepted)"}
    folder = data_dir() / "branding"
    folder.mkdir(parents=True, exist_ok=True)
    for ext in _TYPES:
        (folder / f"logo.{ext}").unlink(missing_ok=True)
    (folder / f"logo.{kind}").write_bytes(data)
    deps.audit_event("branding_logo", target=f"{kind}, {len(data)} bytes", risk="low")
    return {"ok": True, **(await branding())}


@router.delete("/branding/logo")
async def branding_logo_delete() -> dict:
    path = _logo_path()
    if path is not None:
        path.unlink(missing_ok=True)
    return {"ok": True}


@router.post("/branding")
async def branding_save(body: dict) -> dict:
    """Accent colour (``#rrggbb`` or "" for the default) and/or company name."""
    updates: dict = {}
    if "accent" in body:
        accent = str(body.get("accent") or "").strip()
        if accent and not _HEX.fullmatch(accent):
            return {"ok": False, "error": "the accent colour must look like #1a73e8"}
        save_user_config({"ui": {"accent": accent}})
        updates["accent"] = accent
    if "company" in body:
        company = " ".join(str(body.get("company") or "").split())[:80]
        save_user_config({"voip": {"company": company}})
        updates["company"] = company
    deps.audit_event("branding", target=",".join(updates), risk="low")
    return {"ok": True, **(await branding())}


@router.post("/ui/tour")
async def ui_tour(body: dict) -> dict:
    """Remember whether the product tour was completed or skipped (survives devices)."""
    save_user_config({"ui": {"tour_done": bool(body.get("done", True))}})
    return {"ok": True}
