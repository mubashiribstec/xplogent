"""Locate and read the in-app guide pages (``docs/guide/*.md``)."""

from __future__ import annotations

import re
from pathlib import Path

from xplogent.core.repo_info import repo_root

_PACKAGE_FALLBACK = Path(__file__).resolve().parents[3] / "docs" / "guide"


def guide_dir() -> Path | None:
    """Return the directory holding the guide markdown, or None.

    Docker images copy the pages to ``/app/guide`` and point ``XPLOGENT_GUIDE_DIR``
    there (they carry no ``docs/`` folder); a checkout reads ``docs/guide``."""
    import os

    env = os.environ.get("XPLOGENT_GUIDE_DIR", "").strip()
    if env and Path(env).is_dir():
        return Path(env)
    root = repo_root()
    if root and (root / "docs" / "guide").is_dir():
        return root / "docs" / "guide"
    if _PACKAGE_FALLBACK.is_dir():
        return _PACKAGE_FALLBACK
    return None


def _title_of(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return path.stem


def list_pages() -> list[dict[str, str]]:
    d = guide_dir()
    if not d:
        return []
    pages = []
    for path in sorted(d.glob("*.md")):
        slug = re.sub(r"^\d+-", "", path.stem)  # strip the ordering prefix
        pages.append({"slug": slug, "title": _title_of(path), "file": path.name})
    return pages


def read_page(slug: str) -> str | None:
    d = guide_dir()
    if not d:
        return None
    for path in d.glob("*.md"):
        if re.sub(r"^\d+-", "", path.stem) == slug:
            return path.read_text(encoding="utf-8")
    return None
