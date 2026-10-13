"""Per-agent permission profiles (role-based rights).

A :class:`PermissionProfile` captures what an individual agent is allowed to do:
which tools it may call, its per-risk approval policy, which filesystem paths it
may touch, whether it has network access, and its step budget. The orchestrator
assigns a profile (by role) to each worker; the :class:`~xplogent.safety.approval.SafetyManager`
enforces it, and the tool registry is filtered to the allowed tools.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Tools that perform network access (used when a profile sets network=False).
NETWORK_TOOLS = {"web_search", "web_fetch", "browser"}

# Filesystem-writing tools whose path arguments are checked against allowed_paths.
PATH_WRITE_TOOLS = {"write_file", "edit_file", "screenshot", "browser"}


@dataclass
class PermissionProfile:
    name: str = "operator"
    allowed_tools: set[str] | str = "*"          # "*" = all tools
    policy: dict[str, str] = field(
        default_factory=lambda: {
            "low": "auto", "medium": "confirm", "high": "confirm", "critical": "deny"
        }
    )
    allowed_paths: list[str] = field(default_factory=list)  # empty = unrestricted
    network: bool = True
    max_steps: int = 25

    def allows_tool(self, tool_name: str) -> bool:
        if self.allowed_tools == "*":
            if not self.network and tool_name in NETWORK_TOOLS:
                return False
            return True
        allowed = tool_name in self.allowed_tools
        if allowed and not self.network and tool_name in NETWORK_TOOLS:
            return False
        return allowed

    def tool_filter(self) -> set[str] | None:
        """Return the set of allowed tool names, or None for 'all'."""
        if self.allowed_tools == "*":
            return None
        return set(self.allowed_tools)

    @classmethod
    def from_role(cls, role: str, roles_cfg: dict[str, Any]) -> PermissionProfile:
        """Build a profile from a named role in config, falling back to 'operator'."""
        spec = roles_cfg.get(role) or roles_cfg.get("operator") or {}
        allowed = spec.get("allowed_tools", "*")
        if isinstance(allowed, list):
            allowed = set(allowed)
        return cls(
            name=role,
            allowed_tools=allowed,
            policy=spec.get("policy") or cls().policy,
            allowed_paths=spec.get("allowed_paths") or [],
            network=bool(spec.get("network", True)),
            max_steps=int(spec.get("max_steps", 25)),
        )
