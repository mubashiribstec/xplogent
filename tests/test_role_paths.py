"""Audit P2: a role's allowed_paths is a real boundary (reads, listings, writes) and has no shell."""

from __future__ import annotations

import os

from xplogent.core.config import load_config
from xplogent.safety.approval import SafetyManager
from xplogent.safety.profile import PermissionProfile
from xplogent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from xplogent.tools.shell import ShellTool

AUTO = {"low": "auto", "medium": "auto", "high": "auto", "critical": "deny"}


def scoped(tmp_path, tools):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "notes.txt").write_text("ok", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("no", encoding="utf-8")
    prof = PermissionProfile(name="coder", allowed_tools=set(tools), policy=AUTO, allowed_paths=[str(ws)])
    cfg = load_config().safety
    return SafetyManager.from_config(cfg).with_profile(prof, cfg), ws


async def test_reads_and_listings_outside_the_role_paths_are_refused(tmp_path):
    sm, ws = scoped(tmp_path, {"read_file", "list_dir"})
    assert (await sm.evaluate(ReadFileTool(), {"path": str(ws / "notes.txt")})).allowed
    bad = await sm.evaluate(ReadFileTool(), {"path": str(tmp_path / "secret.txt")})
    assert not bad.allowed and "outside" in bad.reason
    assert not (await sm.evaluate(ListDirTool(), {"path": str(tmp_path)})).allowed
    assert not (await sm.evaluate(ReadFileTool(), {"path": str(ws / ".." / "secret.txt")})).allowed


async def test_a_symlink_cannot_escape(tmp_path):
    sm, ws = scoped(tmp_path, {"read_file", "write_file", "edit_file"})
    os.symlink(tmp_path / "secret.txt", ws / "link.txt")
    assert not (await sm.evaluate(ReadFileTool(), {"path": str(ws / "link.txt")})).allowed
    assert not (await sm.evaluate(WriteFileTool(), {"path": str(ws / "link.txt"), "content": "x"})).allowed
    assert not (await sm.evaluate(EditFileTool(), {"path": str(ws / "link.txt"), "old": "no", "new": "x"})).allowed


async def test_shell_is_off_for_a_role_with_paths(tmp_path):
    sm, _ = scoped(tmp_path, {"shell"})
    res = await sm.evaluate(ShellTool(), {"command": "cat ../secret.txt"})
    assert not res.allowed and "shell" in res.reason


async def test_unrestricted_roles_and_the_global_write_roots_keep_their_meaning(tmp_path):
    cfg = load_config().safety
    free = SafetyManager.from_config(cfg).with_profile(PermissionProfile(name="operator", policy=AUTO), cfg)
    assert (await free.evaluate(ReadFileTool(), {"path": str(tmp_path)})).allowed
    glob = SafetyManager(policy=AUTO, allowed_write_roots=[str(tmp_path / "out")])
    assert (await glob.evaluate(ReadFileTool(), {"path": "/etc/hostname"})).allowed          # writes only
    assert not (await glob.evaluate(WriteFileTool(), {"path": "/tmp/x", "content": "x"})).allowed
