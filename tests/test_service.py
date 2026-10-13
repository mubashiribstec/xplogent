"""Background service manager (process spawning mocked)."""

from __future__ import annotations

import pytest

from xplogent.core import service


class _FakeProc:
    def __init__(self, pid=4242, returncode=None):
        self.pid = pid
        self.returncode = returncode

    def poll(self):
        return self.returncode  # None == still alive


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XPLOGENT_HOME", str(tmp_path))
    monkeypatch.setattr(service.time, "sleep", lambda *_: None)  # skip liveness wait
    return tmp_path


def test_start_writes_state(home, monkeypatch):
    monkeypatch.setattr(service, "_can_launch", lambda: (True, ""))
    monkeypatch.setattr(service.subprocess, "Popen", lambda *a, **k: _FakeProc(4242))
    res = service.start(port=9999)
    assert res["ok"] and res["pid"] == 4242
    assert service._read_state()["port"] == 9999


def test_start_detects_immediate_exit(home, monkeypatch):
    """If the detached child dies on startup, start() reports failure with the log."""
    monkeypatch.setattr(service, "_can_launch", lambda: (True, ""))
    monkeypatch.setattr(service.subprocess, "Popen",
                        lambda *a, **k: _FakeProc(4242, returncode=1))
    service._log_path().write_text("UnicodeEncodeError: boom", encoding="utf-8")
    res = service.start(port=9999)
    assert res["ok"] is False
    assert "exited on startup" in res["error"]
    assert "UnicodeEncodeError" in res["log"]
    assert not service._state_path().exists()  # no stale live state


def test_start_uses_utf8_log_and_env(home, monkeypatch):
    """The child is spawned with a UTF-8 logfile and UTF-8 env (the Windows fix)."""
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["env"] = kwargs.get("env")
        captured["stdout_encoding"] = getattr(kwargs.get("stdout"), "encoding", None)
        return _FakeProc(4242)

    monkeypatch.setattr(service, "_can_launch", lambda: (True, ""))
    monkeypatch.setattr(service.subprocess, "Popen", fake_popen)
    service.start(port=9999)
    assert captured["env"]["PYTHONUTF8"] == "1"
    assert captured["env"]["PYTHONIOENCODING"] == "utf-8"
    assert (captured["stdout_encoding"] or "").lower().replace("-", "") == "utf8"


def test_status_running_and_unhealthy(home, monkeypatch):
    monkeypatch.setattr(service, "_can_launch", lambda: (True, ""))
    monkeypatch.setattr(service.subprocess, "Popen", lambda *a, **k: _FakeProc(4242))
    service.start(port=9999)
    monkeypatch.setattr(service, "_pid_alive", lambda pid: True)

    def _boom(*a, **k):
        raise service.httpx.ConnectError("down")
    monkeypatch.setattr(service.httpx, "get", _boom)
    s = service.status()
    assert s["running"] is True
    assert s["healthy"] is False


def test_stop_clears_state(home, monkeypatch):
    monkeypatch.setattr(service, "_can_launch", lambda: (True, ""))
    monkeypatch.setattr(service.subprocess, "Popen", lambda *a, **k: _FakeProc(4242))
    service.start()
    killed = {}
    # Alive until it has been signalled (stop now waits for the exit).
    monkeypatch.setattr(service, "_pid_alive", lambda pid: "pid" not in killed)
    monkeypatch.setattr(service, "is_windows", lambda: False)
    monkeypatch.setattr(service.os, "kill", lambda pid, sig: killed.setdefault("pid", pid))
    res = service.stop()
    assert res["stopped"] is True
    assert killed["pid"] == 4242
    assert not service._state_path().exists()


def test_stop_reports_a_server_that_will_not_exit(home, monkeypatch):
    monkeypatch.setattr(service, "_can_launch", lambda: (True, ""))
    monkeypatch.setattr(service.subprocess, "Popen", lambda *a, **k: _FakeProc(4242))
    service.start()
    monkeypatch.setattr(service, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(service, "is_windows", lambda: False)
    signals = []
    monkeypatch.setattr(service.os, "kill", lambda pid, sig: signals.append(sig))
    res = service.stop(timeout=0.3)
    assert res["ok"] is False
    assert service.signal.SIGKILL in signals  # escalated after SIGTERM
    assert service._state_path().exists()      # still tracked: it's still running


def test_install_service_systemd(home, monkeypatch, tmp_path):
    monkeypatch.setattr(service, "is_windows", lambda: False)
    monkeypatch.setattr(service.Path, "home", classmethod(lambda cls: tmp_path))
    res = service.install_service(port=8765)
    assert res["ok"] and res["kind"] == "systemd-user"
    assert (tmp_path / ".config/systemd/user/xplogent.service").exists()
