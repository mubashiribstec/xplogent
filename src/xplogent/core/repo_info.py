"""Repository location helpers.

Kept from the former self-updater module: other parts of the product
(guide, service, dashboard capabilities, web-dist resolution) need to
know whether this is a git checkout and where its root is. The git-pull
self-update feature itself was removed — Docker installs update with
``xplogent-docker update`` and source installs update with git directly.
"""

from __future__ import annotations

import os
from pathlib import Path


def repo_root() -> Path | None:
    """Return the git repo root containing this package, or None if not a checkout."""
    here = Path(__file__).resolve()
    for parent in [here, *here.parents]:
        if (parent / ".git").exists():
            return parent
    return None


def in_docker() -> bool:
    return os.environ.get("XPLOGENT_IN_DOCKER", "").strip() in ("1", "true", "yes")


def build_info() -> dict[str, str]:
    """Which build is running: ``{version, revision}`` (short git sha, "" if unknown).

    Docker images carry it in ``XPLOGENT_REVISION`` (set at build time); a source
    checkout reads it from git. The dashboard shows it so "am I on the fixed version?"
    has an answer.
    """
    from xplogent import __version__

    version = os.environ.get("XPLOGENT_VERSION", "").strip()
    revision = os.environ.get("XPLOGENT_REVISION", "").strip()
    if revision in ("", "unknown") and (root := repo_root()) is not None:
        import subprocess

        try:
            revision = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True,
                                      text=True, timeout=3).stdout.strip()
        except Exception:  # noqa: BLE001 - no git binary: unknown
            revision = ""
    if revision == "unknown":
        revision = ""
    return {"version": version if version and version != "dev" else __version__, "revision": revision[:7]}
