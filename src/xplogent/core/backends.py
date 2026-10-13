"""Terminal execution backends — where ``shell`` and ``python_exec`` actually run.

By default commands run on the local machine (``LocalBackend``), exactly as
before. Selecting a different backend in config runs them inside a Docker
container or on a remote host over SSH instead — isolating the agent's reach
without changing any tool code. The safety/deny-list still runs *before* a
command is dispatched, so destructive commands are blocked on every backend.

Backends shell out to the system ``docker``/``ssh`` clients (no Python deps); a
missing client yields a clear error instead of a crash.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
import signal
import subprocess
from abc import ABC, abstractmethod
from typing import Any

from xplogent.core.logging import get_logger

_log = get_logger("backends")


class TerminalBackend(ABC):
    name = "base"

    @abstractmethod
    async def run(self, command: str, timeout: int = 120,
                  cwd: str | None = None) -> tuple[int, str, str]:
        """Run a shell command, returning ``(exit_code, stdout, stderr)``."""
        raise NotImplementedError


def group_kwargs() -> dict:
    """Start the child in its own process group so a timeout can kill everything it
    spawned (``sh -c "a | b"``, background jobs), not just the outer shell."""
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def kill_tree(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=10)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    with contextlib.suppress(ProcessLookupError):
        proc.kill()


async def communicate(proc: asyncio.subprocess.Process, timeout: float) -> tuple[bytes, bytes]:
    """``proc.communicate()`` with a timeout; on timeout *or cancellation* the whole
    process group is killed and reaped before the exception propagates."""
    try:
        return await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except BaseException:
        kill_tree(proc)
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(proc.wait(), 5)
        raise


async def _exec(argv: list[str], timeout: int) -> tuple[int, str, str]:
    """Run an argv list, returning ``(exit_code, stdout, stderr)``."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            **group_kwargs(),
        )
    except FileNotFoundError:
        return 127, "", f"'{argv[0]}' was not found on this machine."
    try:
        out, err = await communicate(proc, timeout)
    except TimeoutError:
        return 124, "", f"Command timed out after {timeout}s"
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


class LocalBackend(TerminalBackend):
    name = "local"

    async def run(self, command: str, timeout: int = 120,
                  cwd: str | None = None) -> tuple[int, str, str]:
        try:
            proc = await asyncio.create_subprocess_shell(
                command, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, cwd=cwd, **group_kwargs(),
            )
        except Exception as exc:  # noqa: BLE001
            return 1, "", f"Failed to run command: {exc}"
        try:
            out, err = await communicate(proc, timeout)
        except TimeoutError:
            return 124, "", f"Command timed out after {timeout}s"
        return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


class DockerBackend(TerminalBackend):
    name = "docker"

    def __init__(self, image: str = "python:3.11-slim", container: str = "",
                 workdir: str = "/work") -> None:
        self.image = image
        self.container = container.strip()
        self.workdir = workdir or "/work"

    def argv(self, command: str, cwd: str | None) -> list[str]:
        if self.container:  # exec into a long-lived container
            base = ["docker", "exec"]
            if cwd:
                base += ["-w", cwd]
            return [*base, self.container, "sh", "-lc", command]
        # ephemeral container; mount cwd if provided
        base = ["docker", "run", "--rm"]
        if cwd:
            base += ["-v", f"{cwd}:{self.workdir}", "-w", self.workdir]
        else:
            base += ["-w", self.workdir]
        return [*base, self.image, "sh", "-lc", command]

    async def run(self, command: str, timeout: int = 120,
                  cwd: str | None = None) -> tuple[int, str, str]:
        return await _exec(self.argv(command, cwd), timeout)


class SSHBackend(TerminalBackend):
    name = "ssh"

    def __init__(self, host: str = "", user: str = "", key_path: str = "",
                 port: int = 22) -> None:
        self.host = host
        self.user = user
        self.key_path = key_path
        self.port = int(port or 22)

    def argv(self, command: str, cwd: str | None) -> list[str]:
        opts = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new"]
        if self.port and self.port != 22:
            opts += ["-p", str(self.port)]
        if self.key_path:
            opts += ["-i", self.key_path]
        target = f"{self.user}@{self.host}" if self.user else self.host
        remote = f"cd {shlex.quote(cwd)} && {command}" if cwd else command
        return ["ssh", *opts, target, remote]

    async def run(self, command: str, timeout: int = 120,
                  cwd: str | None = None) -> tuple[int, str, str]:
        if not self.host:
            return 1, "", "SSH backend selected but no host configured (execution.ssh.host)."
        return await _exec(self.argv(command, cwd), timeout)


_cached: tuple[str, TerminalBackend] | None = None


def resolve_backend() -> TerminalBackend:
    """Build (and cache) the backend from the current config; used by the tools."""
    from xplogent.core.config import load_config

    global _cached
    cfg = load_config()
    key = repr(getattr(cfg, "execution", {}) or {})
    if _cached is not None and _cached[0] == key:
        return _cached[1]
    backend = build_backend(cfg)
    _cached = (key, backend)
    return backend


def build_backend(config: Any) -> TerminalBackend:
    """Construct the terminal backend selected by ``config.execution``."""
    execution = getattr(config, "execution", None) or {}
    kind = (execution.get("backend") or "local").lower()
    if kind == "docker":
        d = execution.get("docker") or {}
        return DockerBackend(image=d.get("image", "python:3.11-slim"),
                             container=d.get("container", ""),
                             workdir=d.get("workdir", "/work"))
    if kind == "ssh":
        s = execution.get("ssh") or {}
        return SSHBackend(host=s.get("host", ""), user=s.get("user", ""),
                          key_path=s.get("key_path", ""), port=s.get("port", 22))
    if kind != "local":
        _log.warning("unknown execution backend %r; using local", kind)
    return LocalBackend()
