"""Run Xplogent in the background so it survives closing the terminal.

A lightweight process manager (start/stop/status/restart) plus generators for
real OS services (systemd user unit on Linux, a Scheduled Task on Windows) for
boot/login auto-start.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

from xplogent.core.config import xplogent_home
from xplogent.core.logging import get_logger
from xplogent.core.platform import is_windows

_log = get_logger("service")


def _state_path() -> Path:
    return xplogent_home() / "service.json"


def _log_path() -> Path:
    d = xplogent_home() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d / "server.log"


def _read_state() -> dict:
    p = _state_path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def _workdir() -> Path:
    """A stable working directory for the background server (repo root or home)."""
    from xplogent.core.updater import repo_root

    return repo_root() or xplogent_home()


def _can_launch() -> tuple[bool, str]:
    """Verify ``python -m xplogent`` is importable before spawning a detached child."""
    try:
        proc = subprocess.run([sys.executable, "-c", "import xplogent"],
                              capture_output=True, text=True, timeout=20)
    except (subprocess.SubprocessError, OSError) as exc:
        return False, str(exc)
    if proc.returncode != 0:
        return False, (proc.stderr or "could not import xplogent with this Python").strip()
    return True, ""


def _child_env() -> dict:
    """Environment for a detached child: force UTF-8 I/O.

    Without this, the child's stdout/stderr (redirected to a logfile) defaults to
    the locale encoding (cp1252 on Windows), and printing the rich startup banner
    (emoji + box-drawing) raises UnicodeEncodeError and kills the process.
    """
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def _creationflags() -> int:
    """Windows flags to run detached but with a hidden console (survives shell close)."""
    if is_windows():
        # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP. CREATE_NO_WINDOW keeps a hidden
        # console so console-touching init works, unlike DETACHED_PROCESS which removes it.
        return 0x08000000 | 0x00000200
    return 0


def launch_detached(cmd: list[str]) -> subprocess.Popen:
    """Spawn ``cmd`` detached from the terminal, logging to the server logfile (UTF-8)."""
    kwargs: dict = {"stdin": subprocess.DEVNULL, "cwd": str(_workdir()), "env": _child_env()}
    if is_windows():
        kwargs["creationflags"] = _creationflags()
    else:
        kwargs["start_new_session"] = True
    # UTF-8 logfile so the child never hits UnicodeEncodeError on a redirected stream.
    with open(_log_path(), "a", encoding="utf-8", errors="replace") as logf:  # noqa: SIM115
        kwargs["stdout"] = logf
        kwargs["stderr"] = logf
        return subprocess.Popen(cmd, **kwargs)


def _log_tail(lines: int = 25) -> str:
    try:
        text = _log_path().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if is_windows():
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                                 capture_output=True, text=True)
            return str(pid) in out.stdout
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def start(port: int = 8765, host: str = "127.0.0.1") -> dict:
    """Launch `xplogent up` detached so it survives closing the terminal."""
    state = _read_state()
    if state.get("pid") and _pid_alive(state["pid"]):
        return {"ok": True, "already_running": True, **state}

    ok, err = _can_launch()
    if not ok:
        return {"ok": False, "error": f"cannot launch xplogent with {sys.executable}: {err}"}

    cmd = [sys.executable, "-m", "xplogent", "up",
           "--port", str(port), "--host", host, "--no-browser"]
    proc = launch_detached(cmd)

    state = {"pid": proc.pid, "port": port, "host": host, "started_at": time.time()}
    _state_path().write_text(json.dumps(state))

    # Liveness check: if the child died immediately, surface the real reason
    # instead of falsely reporting success.
    time.sleep(1.2)
    if proc.poll() is not None:
        _state_path().unlink(missing_ok=True)
        return {"ok": False, "error": f"server exited on startup (code {proc.returncode})",
                "log": _log_tail()}
    return {"ok": True, "started": True, **state}


def stop(timeout: float = 15.0) -> dict:
    state = _read_state()
    pid = state.get("pid")
    if not pid or not _pid_alive(pid):
        return {"ok": True, "stopped": False, "reason": "not running"}
    try:
        if is_windows():
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        else:
            os.kill(pid, signal.SIGTERM)
    except OSError as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    # Wait for it to actually exit: a restart that starts the new server while the
    # old one still holds the port fails with "address already in use".
    if not _wait_exit(pid, timeout):
        if not is_windows():
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            _wait_exit(pid, 3.0)
        if _pid_alive(pid):
            return {"ok": False, "error": f"server (pid {pid}) did not exit"}
    _state_path().unlink(missing_ok=True)
    return {"ok": True, "stopped": True}


def _wait_exit(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.2)
    return not _pid_alive(pid)


def status() -> dict:
    state = _read_state()
    pid = state.get("pid")
    running = bool(pid and _pid_alive(pid))
    healthy = False
    if running:
        try:
            r = httpx.get(f"http://{state.get('host', '127.0.0.1')}:{state.get('port', 8765)}/health",
                          timeout=2)
            healthy = r.status_code == 200
        except httpx.HTTPError:
            healthy = False
    return {"running": running, "healthy": healthy, **state}


def restart(port: int = 8765, host: str = "127.0.0.1") -> dict:
    stopped = stop()
    if not stopped.get("ok"):
        return stopped
    return start(port=port, host=host)


# ── OS service generators (boot/login auto-start) ─────────────────────────────
def install_service(port: int = 8765) -> dict:
    """Generate (and best-effort register) an OS service for auto-start."""
    exe = sys.executable
    home = xplogent_home()
    workdir = _workdir()
    if is_windows():
        task = "XplogentServer"
        # Point the task at a .bat so we avoid schtasks /TR quoting pitfalls and can
        # set the working directory reliably.
        bat = home / "xplogent-start.bat"
        bat.write_text(
            "@echo off\r\n"
            f'cd /d "{workdir}"\r\n'
            f'"{exe}" -m xplogent up --port {port} --no-browser\r\n',
            encoding="utf-8",
        )
        register = ["schtasks", "/Create", "/TN", task, "/SC", "ONLOGON",
                    "/TR", str(bat), "/F"]
        try:
            subprocess.run(register, capture_output=True, text=True, check=True)
            # Verify it registered.
            subprocess.run(["schtasks", "/Query", "/TN", task],
                           capture_output=True, text=True, check=True)
            return {"ok": True, "kind": "scheduled-task", "name": task, "script": str(bat)}
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            return {"ok": False, "kind": "scheduled-task",
                    "hint": "run this in an elevated PowerShell:", "command": " ".join(register),
                    "error": str(exc)}
    # Linux: systemd user unit
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit = unit_dir / "xplogent.service"
    env_file = home / ".env"
    unit.write_text(
        "[Unit]\nDescription=Xplogent agent server\nAfter=network.target\n\n"
        "[Service]\n"
        f"WorkingDirectory={workdir}\n"
        f"EnvironmentFile=-{env_file}\n"
        f"ExecStart={exe} -m xplogent up --port {port} --no-browser\n"
        "Restart=on-failure\n\n"
        "[Install]\nWantedBy=default.target\n"
    )
    return {"ok": True, "kind": "systemd-user", "unit": str(unit),
            "next": "systemctl --user enable --now xplogent  (and: loginctl enable-linger)"}


def uninstall_service() -> dict:
    if is_windows():
        subprocess.run(["schtasks", "/Delete", "/TN", "XplogentServer", "/F"],
                       capture_output=True)
        return {"ok": True}
    subprocess.run(["systemctl", "--user", "disable", "--now", "xplogent"],
                   capture_output=True)
    unit = Path.home() / ".config" / "systemd" / "user" / "xplogent.service"
    unit.unlink(missing_ok=True)
    return {"ok": True}
