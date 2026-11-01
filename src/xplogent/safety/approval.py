"""Risk classification + approval policy.

Every tool call is routed through :class:`SafetyManager` before it runs. The
tool declares a base risk; the manager may escalate it (e.g. a shell command
matching a deny pattern becomes ``critical``) and then applies the configured
policy: ``auto`` (run), ``confirm`` (ask the user), or ``deny`` (block).
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from xplogent.safety.commands import is_catastrophic
from xplogent.safety.profile import (
    PATH_READ_TOOLS,
    PATH_WRITE_TOOLS,
    UNCONFINABLE_TOOLS,
    PermissionProfile,
)


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


_ORDER = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2, RiskLevel.CRITICAL: 3}


def max_risk(a: RiskLevel, b: RiskLevel) -> RiskLevel:
    return a if _ORDER[a] >= _ORDER[b] else b


@dataclass
class ApprovalRequest:
    tool: str
    arguments: dict[str, Any]
    risk: RiskLevel
    reason: str = ""


@dataclass
class ApprovalDecision:
    allowed: bool
    risk: RiskLevel
    reason: str = ""
    needed_confirmation: bool = False


# An interface supplies this; returns True to allow a "confirm"-tier action.
ApprovalCallback = Callable[[ApprovalRequest], Awaitable[bool]]


_DEFAULT_DENY = [
    # POSIX
    r"rm\s+-rf\s+/",
    r"mkfs",
    r":\(\)\s*\{.*\};:",
    r"dd\s+if=.*of=/dev/",
    # Windows
    r"format\s+[a-z]:",
    r"del\s+/[sf]",
    r"rd\s+/s",
    r"rmdir\s+/s",
    r"diskpart",
]


def _merge_patterns(user: list[str] | None, base: list[str] | None = None) -> list[str]:
    """User deny patterns *extend* the built-ins; they never replace them."""
    out = list(base if base is not None else _DEFAULT_DENY)
    for p in user or []:
        if p not in out:
            out.append(p)
    return out


def auto_approve_up_to_high() -> ApprovalCallback:
    """Approver for unattended runs (background/API): everything but CRITICAL."""

    async def approve(req: ApprovalRequest) -> bool:
        return req.risk != RiskLevel.CRITICAL

    return approve


@dataclass
class SafetyManager:
    policy: dict[str, str] = field(
        default_factory=lambda: {
            "low": "auto",
            "medium": "confirm",
            "high": "confirm",
            "critical": "deny",
        }
    )
    deny_patterns: list[str] = field(default_factory=lambda: list(_DEFAULT_DENY))
    allowed_write_roots: list[str] = field(default_factory=list)
    # A role's allowed_paths: a boundary for reads and listings too (and no shell).
    allowed_read_roots: list[str] = field(default_factory=list)
    # Optional per-agent profile. When set, it overrides policy and adds
    # tool allow-listing + filesystem path scoping.
    profile: PermissionProfile | None = None

    @classmethod
    def from_config(cls, safety_cfg: dict[str, Any]) -> SafetyManager:
        return cls(
            policy=safety_cfg.get("policy") or cls().policy,
            deny_patterns=_merge_patterns(safety_cfg.get("deny_patterns")),
            allowed_write_roots=safety_cfg.get("allowed_write_roots") or [],
        )

    def with_profile(self, profile: PermissionProfile, safety_cfg: dict[str, Any]) -> SafetyManager:
        """Return a SafetyManager scoped to a per-agent permission profile."""
        return SafetyManager(
            policy=profile.policy or self.policy,
            deny_patterns=_merge_patterns(safety_cfg.get("deny_patterns"), self.deny_patterns),
            allowed_write_roots=profile.allowed_paths or self.allowed_write_roots,
            allowed_read_roots=list(profile.allowed_paths or []),
            profile=profile,
        )

    def _matches_deny(self, arguments: dict[str, Any]) -> bool:
        blob = " ".join(str(v) for v in arguments.values())
        # Built-in detection of system/home wipes in any spelling (rm -fr /, rm -r -f ~,
        # rm --recursive --force /, shutil.rmtree('/'), mkfs, dd of=/dev/sda, fork bomb...)
        # plus the user's own regexes.
        if is_catastrophic(blob):
            return True
        for p in self.deny_patterns:
            try:
                if re.search(p, blob, re.IGNORECASE):
                    return True
            except re.error:
                continue  # a malformed user pattern must not disable the rest
        return False

    def _path_violation(self, tool_name: str, arguments: dict[str, Any]) -> str | None:
        """A reason string if the call leaves this agent's paths, else None.

        Writes are checked against ``allowed_write_roots``; with a role's ``allowed_paths``,
        reads and listings are checked too, and shell is refused (argument checks can't
        confine a shell command: that needs an OS/container sandbox)."""
        if self.allowed_read_roots and tool_name in UNCONFINABLE_TOOLS:
            return (f"'{tool_name}' is off for this role: its allowed paths can't be enforced on shell "
                    "commands (run the agent in a container, or remove allowed_paths)")
        if tool_name in PATH_WRITE_TOOLS:
            roots = self.allowed_write_roots
        elif tool_name in PATH_READ_TOOLS:
            roots = self.allowed_read_roots
        else:
            return None
        if not roots:
            return None
        target = arguments.get("path") or ("." if tool_name == "list_dir" else "")
        if not target:
            return None
        try:
            resolved = Path(str(target)).expanduser().resolve()   # symlinks resolved: no escape
        except (OSError, RuntimeError):
            return f"invalid path: {target}"
        for root in roots:
            try:
                if resolved.is_relative_to(Path(root).expanduser().resolve()):
                    return None
            except (OSError, RuntimeError):
                continue
        return f"path '{resolved}' is outside this agent's allowed paths"

    def classify(self, tool: Any, arguments: dict[str, Any]) -> tuple[RiskLevel, str]:
        """Determine the effective risk of a tool call."""
        base = tool.risk_for(arguments) if hasattr(tool, "risk_for") else RiskLevel.MEDIUM
        reason = ""
        if self._matches_deny(arguments):
            return RiskLevel.CRITICAL, "matches a configured deny pattern"
        return base, reason

    async def evaluate(
        self,
        tool: Any,
        arguments: dict[str, Any],
        approve: ApprovalCallback | None = None,
    ) -> ApprovalDecision:
        tool_name = getattr(tool, "name", str(tool))

        # 1. Tool allow-listing per the agent's profile.
        if self.profile is not None and not self.profile.allows_tool(tool_name):
            return ApprovalDecision(
                False, RiskLevel.HIGH,
                f"tool '{tool_name}' is not permitted for role '{self.profile.name}'",
            )

        # 2. Filesystem path scoping.
        if violation := self._path_violation(tool_name, arguments):
            return ApprovalDecision(False, RiskLevel.HIGH, violation)

        risk, reason = self.classify(tool, arguments)
        action = self.policy.get(risk.value, "confirm")

        if action == "deny":
            return ApprovalDecision(False, risk, reason or "blocked by policy")
        if action == "auto":
            return ApprovalDecision(True, risk, reason)

        # action == "confirm"
        if approve is None:
            # No interactive approver available → fail safe by blocking.
            return ApprovalDecision(False, risk, "confirmation required but no approver")
        ok = await approve(ApprovalRequest(tool=getattr(tool, "name", str(tool)),
                                           arguments=arguments, risk=risk, reason=reason))
        return ApprovalDecision(ok, risk, reason, needed_confirmation=True)
