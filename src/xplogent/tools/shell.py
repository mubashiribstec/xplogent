"""Shell command execution (via the configured terminal backend)."""

from __future__ import annotations

from typing import Any

from xplogent.core.backends import TerminalBackend, resolve_backend
from xplogent.safety.approval import RiskLevel
from xplogent.safety.commands import classify_command
from xplogent.tools.base import Tool, ToolResult


class ShellTool(Tool):
    name = "shell"
    description = (
        "Run a shell command on the local machine and return its stdout/stderr. "
        "Use for system tasks, running programs, git, package managers, etc."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The shell command to execute."},
            "timeout": {"type": "integer", "description": "Timeout in seconds (default 120)."},
            "cwd": {"type": "string", "description": "Working directory (optional)."},
        },
        "required": ["command"],
    }
    risk = RiskLevel.HIGH

    def __init__(self, backend: TerminalBackend | None = None) -> None:
        self._backend = backend

    def risk_for(self, arguments: dict[str, Any]) -> RiskLevel:
        # Only a single, simple, read-only command auto-runs; pipes/chains/redirects,
        # interpreters, network and destructive programs need approval.
        return RiskLevel(classify_command(str(arguments.get("command", ""))))

    async def run(self, command: str, timeout: int = 120, cwd: str | None = None) -> ToolResult:
        backend = self._backend or resolve_backend()
        code, out, err = await backend.run(command, timeout=timeout, cwd=cwd)
        body = out
        if err:
            body += ("\n[stderr]\n" + err)
        body = f"(exit {code})\n{body}".strip()
        return ToolResult(ok=code == 0, output=body, error="" if code == 0 else body,
                          data={"exit_code": code, "backend": backend.name})
