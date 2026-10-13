"""Execute Python code via the configured terminal backend and capture output."""

from __future__ import annotations

import asyncio
import base64
import sys

from xplogent.core.backends import (
    TerminalBackend,
    communicate,
    group_kwargs,
    resolve_backend,
)
from xplogent.safety.approval import RiskLevel
from xplogent.tools.base import Tool, ToolResult


class PythonExecTool(Tool):
    name = "python_exec"
    description = (
        "Execute a snippet of Python code in a fresh subprocess and return whatever "
        "it prints to stdout/stderr. Good for calculations, data work, and quick scripts."
    )
    parameters = {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Python source to run."},
            "timeout": {"type": "integer", "description": "Timeout in seconds (default 60)."},
        },
        "required": ["code"],
    }
    risk = RiskLevel.HIGH

    def __init__(self, backend: TerminalBackend | None = None) -> None:
        self._backend = backend

    async def run(self, code: str, timeout: int = 60) -> ToolResult:
        backend = self._backend or resolve_backend()
        if backend.name == "local":
            return await self._run_local(code, timeout)
        # Docker/SSH: ship the code base64-encoded so quoting/newlines survive.
        b64 = base64.b64encode(code.encode("utf-8")).decode("ascii")
        command = f"echo {b64} | base64 -d | python3"
        rc, out, err = await backend.run(command, timeout=timeout)
        body = out + (("\n[stderr]\n" + err) if err else "")
        ok = rc == 0
        return ToolResult(ok=ok, output=body.strip(),
                          error="" if ok else (err.strip() or f"non-zero exit ({rc})"),
                          data={"backend": backend.name})

    async def _run_local(self, code: str, timeout: int) -> ToolResult:
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-c", code,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **group_kwargs(),
            )
            try:
                stdout, stderr = await communicate(proc, timeout)
            except TimeoutError:
                return ToolResult.failure(f"Execution timed out after {timeout}s")
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"Failed to execute: {exc}")

        out = stdout.decode("utf-8", "replace")
        err = stderr.decode("utf-8", "replace")
        body = out + (("\n[stderr]\n" + err) if err else "")
        code_ok = (proc.returncode or 0) == 0
        return ToolResult(ok=code_ok, output=body.strip(),
                          error="" if code_ok else err.strip() or "non-zero exit")
