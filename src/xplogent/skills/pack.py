"""SKILL.md format — a skill authored as YAML frontmatter + a markdown body.

Frontmatter fields: ``name``, ``description``, ``tools`` (list of required tool
names), ``trigger`` (when to use it). The body is the step-by-step procedure. This
is OpenClaw's skill-as-markdown pattern, mapped onto Xplogent's skill store.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


def _norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_") or "skill"


def parse_skill_md(text: str) -> dict[str, Any]:
    """Parse a SKILL.md into ``{name, description, trigger, tools, body}``."""
    name = description = trigger = ""
    tools: list[str] = []
    body = text.strip()
    m = _FRONTMATTER.match(text)
    if m:
        try:
            meta = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            meta = {}
        if not isinstance(meta, dict):
            meta = {}  # frontmatter that's a list/scalar is not metadata
        body = m.group(2).strip()
        name = str(meta.get("name", "")).strip()
        description = str(meta.get("description", "")).strip()
        trigger = str(meta.get("trigger", "")).strip()
        raw_tools = meta.get("tools") or []
        if isinstance(raw_tools, list):
            tools = [str(t).strip() for t in raw_tools if str(t).strip()]
        else:
            tools = [t.strip() for t in str(raw_tools).split(",") if t.strip()]
    if not name:
        heading = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
        name = heading.group(1).strip() if heading else "skill"
    if not description:
        quote = re.search(r"^>\s*(.+)$", body, re.MULTILINE)
        description = quote.group(1).strip() if quote else ""
    return {"name": _norm_name(name), "description": description,
            "trigger": trigger, "tools": tools, "body": body}


def render_skill_md(name: str, description: str, body: str,
                    tools: list[str] | None = None, trigger: str = "") -> str:
    """Render a skill back to SKILL.md (frontmatter + body)."""
    fm: dict[str, Any] = {"name": name, "description": description}
    if trigger:
        fm["trigger"] = trigger
    if tools:
        fm["tools"] = tools
    front = yaml.safe_dump(fm, sort_keys=False).strip()
    return f"---\n{front}\n---\n\n{body.strip()}\n"
