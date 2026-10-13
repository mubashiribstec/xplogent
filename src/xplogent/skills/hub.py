"""Skills hub — install ready-made skill packs (SKILL.md) from the bundled library,
a local path, or an http(s) URL. ClawHub-style, but local-first."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from xplogent.core.config import install_root, load_config
from xplogent.core.logging import get_logger
from xplogent.memory.manager import MemoryManager
from xplogent.skills.pack import parse_skill_md, render_skill_md

_log = get_logger("skills.hub")


def library_dir() -> Path | None:
    """The starter skill library, shipped inside the package (works for all installs)."""
    packaged = Path(__file__).resolve().parent.parent / "skills_library"
    if packaged.is_dir():
        return packaged
    root = install_root()  # dev/editable fallback
    d = (root / "skills_library") if root else None
    return d if (d and d.is_dir()) else None


def list_bundled() -> list[dict[str, Any]]:
    """The starter skill packs shipped with the install."""
    d = library_dir()
    out: list[dict[str, Any]] = []
    if not d:
        return out
    for p in sorted(d.glob("*/SKILL.md")) + sorted(d.glob("*.md")):
        try:
            meta = parse_skill_md(p.read_text(encoding="utf-8"))
        except OSError:
            continue
        out.append({"name": meta["name"], "description": meta["description"],
                    "trigger": meta["trigger"], "tools": meta["tools"], "path": str(p)})
    return out


_MAX_SKILL_BYTES = 256_000


def _read_md(path: Path) -> str:
    """Only Markdown skill files, size-capped — so "install a skill from ~/.ssh/id_rsa"
    can't pull arbitrary local files into the knowledge base (and out via export)."""
    if path.suffix.lower() != ".md":
        raise ValueError(f"skills must be .md files (got {path.name})")
    if path.stat().st_size > _MAX_SKILL_BYTES:
        raise ValueError(f"{path.name} is larger than {_MAX_SKILL_BYTES // 1000} KB")
    return path.read_text(encoding="utf-8")


def _collect(src: str) -> list[str]:
    """Return SKILL.md texts for a URL, file, directory, or a bundled pack name."""
    p = Path(src).expanduser()
    if p.is_file():
        return [_read_md(p)]
    if p.is_dir():
        files = sorted(p.glob("*/SKILL.md")) + sorted(p.glob("*.md"))
        return [_read_md(f) for f in files]
    # a bundled pack name
    lib = library_dir()
    if lib:
        for cand in (lib / src / "SKILL.md", lib / f"{src}.md"):
            if cand.is_file():
                return [cand.read_text(encoding="utf-8")]
    raise FileNotFoundError(f"no skill pack found at '{src}'")


async def install_text(skill_md: str, memory: MemoryManager) -> dict[str, Any]:
    """Install a single SKILL.md given as text."""
    meta = parse_skill_md(skill_md)
    await memory.save_skill(meta["name"], meta["description"], meta["body"],
                            tools=meta["tools"], trigger=meta["trigger"], source="installed")
    _write_md(meta)
    return {"ok": True, "installed": [meta["name"]]}


async def install_pack(src: str, memory: MemoryManager) -> dict[str, Any]:
    """Install one or many skills from a URL, path, or bundled pack name."""
    if src.startswith(("http://", "https://")):
        from xplogent.core.netguard import safe_get

        _final, text = await safe_get(src, max_bytes=_MAX_SKILL_BYTES)  # SSRF-checked
        texts = [text]
    else:
        texts = await asyncio.to_thread(_collect, src)  # file IO off the loop
    installed: list[str] = []
    for text in texts:
        meta = parse_skill_md(text)
        await memory.save_skill(meta["name"], meta["description"], meta["body"],
                                tools=meta["tools"], trigger=meta["trigger"], source="installed")
        _write_md(meta)
        installed.append(meta["name"])
    return {"ok": True, "installed": installed}


def _write_md(meta: dict[str, Any]) -> None:
    skills_dir = load_config().skills_dir
    try:
        (skills_dir / f"{meta['name']}.md").write_text(
            render_skill_md(meta["name"], meta["description"], meta["body"],
                            meta["tools"], meta["trigger"]),
            encoding="utf-8",
        )
    except OSError as exc:  # noqa: BLE001
        _log.warning("could not write skill markdown: %s", exc)
