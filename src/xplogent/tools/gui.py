"""GUI automation — mouse, keyboard, and screenshots (full PC control).

Backed by ``pyautogui`` (input) and ``mss`` (screen capture). These need a real
display; on a headless/remote machine they degrade gracefully with a clear
message instead of crashing.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from xplogent.core.config import xplogent_home
from xplogent.core.platform import has_display as _has_display
from xplogent.safety.approval import RiskLevel
from xplogent.tools.base import Tool, ToolResult, optional_import_error


class ScreenshotTool(Tool):
    name = "screenshot"
    description = (
        "Capture the screen to a PNG file and return its path. Use before GUI "
        "actions so a vision-capable model can see the screen."
    )
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Optional output path."}},
    }
    risk = RiskLevel.LOW

    def risk_for(self, arguments: dict[str, Any]) -> RiskLevel:
        # Writing to a caller-chosen path can overwrite any file (e.g. ~/.bashrc).
        return RiskLevel.MEDIUM if arguments.get("path") else RiskLevel.LOW

    async def run(self, path: str | None = None) -> ToolResult:
        if not _has_display():
            return ToolResult.failure("No display available (headless environment).")
        try:
            import mss  # type: ignore
        except ImportError:
            return optional_import_error("mss", "control")
        out = Path(path).expanduser() if path else xplogent_home() / f"screenshot_{int(time.time())}.png"
        try:
            with mss.mss() as sct:
                sct.shot(output=str(out))
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"Screenshot failed: {exc}")
        return ToolResult.success(f"Saved screenshot to {out}", path=str(out))


class MouseTool(Tool):
    name = "mouse"
    description = "Control the mouse: move, click, double-click, right-click, or scroll."
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["move", "click", "double_click",
                                                   "right_click", "scroll"]},
            "x": {"type": "integer"},
            "y": {"type": "integer"},
            "amount": {"type": "integer", "description": "Scroll amount (for action=scroll)."},
        },
        "required": ["action"],
    }
    risk = RiskLevel.HIGH

    async def run(self, action: str, x: int | None = None, y: int | None = None,
                  amount: int = 0) -> ToolResult:
        pg = _load_pyautogui()
        if isinstance(pg, ToolResult):
            return pg
        try:
            if action == "move":
                pg.moveTo(x, y, duration=0.2)
            elif action == "click":
                pg.click(x, y)
            elif action == "double_click":
                pg.doubleClick(x, y)
            elif action == "right_click":
                pg.rightClick(x, y)
            elif action == "scroll":
                pg.scroll(amount)
            else:
                return ToolResult.failure(f"Unknown mouse action: {action}")
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"Mouse action failed: {exc}")
        return ToolResult.success(f"mouse {action} done")


class KeyboardTool(Tool):
    name = "keyboard"
    description = "Type text or press a key combination (e.g. 'ctrl+c', 'enter')."
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Text to type."},
            "hotkey": {"type": "string", "description": "Keys joined by '+', e.g. 'ctrl+s'."},
        },
    }
    risk = RiskLevel.HIGH

    async def run(self, text: str | None = None, hotkey: str | None = None) -> ToolResult:
        pg = _load_pyautogui()
        if isinstance(pg, ToolResult):
            return pg
        try:
            if hotkey:
                pg.hotkey(*[k.strip() for k in hotkey.split("+")])
            if text:
                pg.typewrite(text, interval=0.02)
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"Keyboard action failed: {exc}")
        return ToolResult.success("keyboard input sent")


def _load_pyautogui():
    if not _has_display():
        return ToolResult.failure("No display available (headless environment).")
    try:
        import pyautogui  # type: ignore
        pyautogui.FAILSAFE = True
        return pyautogui
    except ImportError:
        return optional_import_error("pyautogui", "control")
    except Exception as exc:  # noqa: BLE001
        return ToolResult.failure(f"GUI automation unavailable: {exc}")


def gui_tools() -> list[Tool]:
    return [ScreenshotTool(), MouseTool(), KeyboardTool()]
