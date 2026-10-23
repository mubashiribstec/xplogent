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
