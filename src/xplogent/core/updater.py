"""One-click self-update for git-clone / editable installs.

Detects the repository, checks whether the tracked branch is behind its remote,
pulls fast-forward, reinstalls (in case dependencies changed), and can re-exec
the process so the new code takes effect.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from xplogent.core.logging import get_logger

_log = get_logger("updater")


def repo_root() -> Path | None:
    """Return the git repo root containing this package, or None if not a checkout."""
    here = Path(__file__).resolve()
    for parent in [here, *here.parents]:
        if (parent / ".git").exists():
            return parent
    return None


GIT_TIMEOUT = 120
PIP_TIMEOUT = 900


def _git(root: Path, *args: str, timeout: float = GIT_TIMEOUT) -> tuple[int, str]:
    # Never wait on a credential prompt nobody can answer (the server has no TTY).
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(root), capture_output=True, text=True,
            env=env, timeout=timeout, stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return 124, f"git {args[0] if args else ''} timed out after {timeout:.0f}s"
    except OSError as exc:
        return 127, f"git not runnable: {exc}"
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def current_commit(root: Path | None = None) -> str:
    root = root or repo_root()
    if not root:
        return ""
    _, out = _git(root, "rev-parse", "--short", "HEAD")
    return out


def check_update() -> dict:
    """Compare the local branch against its upstream. Network: runs ``git fetch``."""
    root = repo_root()
    if not root:
        return {"git": False, "update_available": False,
                "error": "not a git checkout (installed from a package)"}

    _git(root, "fetch", "--quiet")
    rc, branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    upstream = f"origin/{branch}"
    rc, counts = _git(root, "rev-list", "--left-right", "--count", f"HEAD...{upstream}")
    behind = 0
    if rc == 0 and counts:
        parts = counts.split()
        behind = int(parts[1]) if len(parts) == 2 else 0
    _, changelog = _git(root, "log", "--oneline", f"HEAD..{upstream}")
    return {
        "git": True,
        "update_available": behind > 0,
        "behind_by": behind,
        "current": current_commit(root),
        "branch": branch,
        "changelog": changelog,
    }


def pull() -> dict:
    """Fast-forward pull. Returns success + combined git output."""
    root = repo_root()
    if not root:
        return {"ok": False, "output": "not a git checkout"}
    rc, out = _git(root, "pull", "--ff-only")
    return {"ok": rc == 0, "output": out, "root": str(root)}


def reinstall() -> dict:
    """Reinstall the package so changed dependencies are picked up."""
    root = repo_root()
    if not root:
        return {"ok": False, "output": "not a git checkout"}
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-e", ".", "--quiet"],
            cwd=str(root), capture_output=True, text=True, timeout=PIP_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": f"pip install timed out after {PIP_TIMEOUT}s"}
    except OSError as exc:
        return {"ok": False, "output": str(exc)}
    return {"ok": proc.returncode == 0, "output": (proc.stdout + proc.stderr).strip()}


def rebuild_web() -> dict:
    """Rebuild the dashboard so GUI changes actually deploy after an update.

    Best-effort: a no-op (with a note) when Node/npm isn't installed.
    """
    import shutil

    root = repo_root()
    web = root / "web" if root else None
    if not web or not web.is_dir():
        return {"ok": True, "skipped": "no web dir"}
    if shutil.which("npm") is None:
        return {"ok": True, "skipped": "npm not found — dashboard not rebuilt"}
    try:
        subprocess.run(["npm", "install", "--no-audit", "--no-fund"],
                       cwd=str(web), capture_output=True, text=True, timeout=600)
        proc = subprocess.run(["npm", "run", "build"], cwd=str(web),
                              capture_output=True, text=True, timeout=600)
        return {"ok": proc.returncode == 0, "output": (proc.stdout + proc.stderr).strip()[-2000:]}
    except (subprocess.SubprocessError, OSError) as exc:
        return {"ok": False, "output": str(exc)}


def restart(extra_args: list[str] | None = None) -> None:
    """Restart the process so new code loads. Does not return on success.

    POSIX re-execs in place; on Windows ``os.execv`` loses the console/service
    context, so we spawn a fresh detached process and exit this one.
    """
    args = [sys.executable, "-m", "xplogent", *(extra_args or ["up"])]
    if not Path(sys.executable).exists():
        _log.error("cannot restart: python executable missing at %s", sys.executable)
        return
    _log.info("restarting: %s", " ".join(args))
    if os.name == "nt":
        # Re-launch detached with a hidden console + UTF-8 env (same safe path as
        # `xplogent start`), then exit this process. A raw DETACHED_PROCESS Popen here
        # would inherit the cp1252 crash on the rich startup banner.
        from xplogent.core import service
        service.launch_detached(args)
        os._exit(0)
    os.execv(sys.executable, args)


def update_and_restart(extra_args: list[str] | None = None) -> dict:
    """Backup → pull → reinstall → rebuild dashboard → restart. Returns a report.

    On success the process restarts and this never returns; on any earlier failure
    it returns a dict describing the stage that failed (no restart).
    """
    report: dict = {}
    try:
        from xplogent.core.backup import create_backup

        report["backup"] = create_backup().get("path")
    except Exception as exc:  # noqa: BLE001 - backup failure shouldn't block, just note it
        report["backup_error"] = str(exc)

    pulled = pull()
    report["pull"] = pulled["output"]
    if not pulled["ok"]:
        return {"ok": False, "stage": "pull", **report}
    report["install"] = reinstall()["output"]
    web = rebuild_web()
    report["web"] = web.get("output") or web.get("skipped")
    restart(extra_args)  # does not return on success
    return {"ok": True, "restarting": True, **report}
