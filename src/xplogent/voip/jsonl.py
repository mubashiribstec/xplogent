"""Small append-only JSON-lines logs that can't grow without bound.

Used for the CRM delivery log and the knowledge-gap log: one JSON object per
line, newest last. When a file passes ``max_bytes`` its oldest half is dropped,
so a busy line (or a CRM that's down for a week) never fills the disk.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

MAX_BYTES = 2_000_000


def append(path: Path, record: dict[str, Any], *, max_bytes: int = MAX_BYTES) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    if path.stat().st_size > max_bytes:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("".join(lines[len(lines) // 2:]), encoding="utf-8")
        tmp.replace(path)


def read(path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    """Records oldest first (the last ``limit`` when given); corrupt lines are skipped."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-limit:] if limit else lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out
