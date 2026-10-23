"""Per-agent permission profiles: tool allow-listing and path scoping."""

from __future__ import annotations

import pytest

from xplogent.core.config import load_config
from xplogent.safety.approval import SafetyManager
from xplogent.safety.profile import PermissionProfile
from xplogent.tools.filesystem import WriteFileTool
from xplogent.tools.registry import ToolRegistry
from xplogent.tools.shell import ShellTool


def _profile(role: str) -> PermissionProfile:
    return PermissionProfile.from_role(role, load_config().roles)


def test_researcher_role_blocks_shell():
    assert _profile("researcher").allows_tool("shell") is False
    assert _profile("researcher").allows_tool("read_file") is True
    assert _profile("researcher").allows_tool("write_file") is False


def test_registry_filter_respects_allowed_tools():
    reg = ToolRegistry.from_config()
    prof = _profile("reviewer")
    filtered = reg.filtered(prof.tool_filter())
    names = {t.name for t in filtered.all()}
    assert "read_file" in names
    assert "shell" not in names
    assert "write_file" not in names


@pytest.mark.asyncio
async def test_safety_blocks_disallowed_tool():
    base = SafetyManager.from_config(load_config().safety)
    sm = base.with_profile(_profile("researcher"), load_config().safety)
    decision = await sm.evaluate(ShellTool(), {"command": "ls"})
    assert decision.allowed is False
    assert "not permitted" in decision.reason


@pytest.mark.asyncio
async def test_path_scoping_blocks_outside_write(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    prof = PermissionProfile(
        name="coder",
        allowed_tools={"write_file"},
        policy={"low": "auto", "medium": "auto", "high": "auto", "critical": "deny"},
        allowed_paths=[str(workspace)],
    )
    sm = SafetyManager.from_config(load_config().safety).with_profile(prof, load_config().safety)

    # inside the sandbox → allowed
    ok = await sm.evaluate(WriteFileTool(), {"path": str(workspace / "a.txt"), "content": "x"})
    assert ok.allowed is True

    # outside the sandbox → blocked
    bad = await sm.evaluate(WriteFileTool(), {"path": str(tmp_path / "escape.txt"), "content": "x"})
    assert bad.allowed is False
    assert "outside" in bad.reason
