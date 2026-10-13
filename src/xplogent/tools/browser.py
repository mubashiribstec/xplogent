"""Headless/headful browser control via Playwright.

Keeps one browser+page alive across calls so the agent can navigate, read,
click, and type across a multi-step web task.
"""

from __future__ import annotations

from typing import Any

from xplogent.safety.approval import RiskLevel
from xplogent.tools.base import Tool, ToolResult, optional_import_error


class _BrowserSession:
    """Lazily-started shared Playwright browser/page."""

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self.page = None

    async def ensure(self, headless: bool = True):
        try:
            from playwright.async_api import async_playwright  # type: ignore
        except ImportError:
            return optional_import_error("playwright", "control")
        if self.page is not None:
            return self.page
        try:
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(headless=headless)
            self.page = await self._browser.new_page()
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(
                f"Could not start browser: {exc}. Run 'playwright install chromium' first."
            )
        return self.page

    async def close(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        self._pw = self._browser = self.page = None


_SESSION = _BrowserSession()


class BrowserTool(Tool):
    name = "browser"
    description = (
        "Drive a real web browser. Actions: 'goto' (url), 'read' (visible text), "
        "'click' (selector), 'type' (selector+text), 'screenshot' (path)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["goto", "read", "click", "type", "screenshot"]},
            "url": {"type": "string"},
            "selector": {"type": "string", "description": "CSS selector."},
            "text": {"type": "string"},
            "path": {"type": "string"},
        },
        "required": ["action"],
    }
    risk = RiskLevel.MEDIUM

    def risk_for(self, arguments: dict[str, Any]) -> RiskLevel:
        action = arguments.get("action")
        if action == "screenshot" and arguments.get("path"):
            return RiskLevel.MEDIUM  # caller-chosen path can overwrite any file
        return RiskLevel.LOW if action in {"goto", "read", "screenshot"} else RiskLevel.MEDIUM

    async def run(self, action: str, url: str | None = None, selector: str | None = None,
                  text: str | None = None, path: str | None = None) -> ToolResult:
        page = await _SESSION.ensure()
        if isinstance(page, ToolResult):
            return page
        try:
            if action == "goto":
                if not url:
                    return ToolResult.failure("goto requires 'url'")
                await page.goto(url, wait_until="domcontentloaded")
                return ToolResult.success(f"Navigated to {url} (title: {await page.title()})")
            if action == "read":
                body = await page.inner_text("body")
                return ToolResult.success(body[:8000])
            if action == "click":
                await page.click(selector)
                return ToolResult.success(f"Clicked {selector}")
            if action == "type":
                await page.fill(selector, text or "")
                return ToolResult.success(f"Typed into {selector}")
            if action == "screenshot":
                out = path or "page.png"
                await page.screenshot(path=out)
                return ToolResult.success(f"Saved {out}", path=out)
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"Browser action failed: {exc}")
        return ToolResult.failure(f"Unknown action: {action}")


def browser_tools() -> list[Tool]:
    return [BrowserTool()]
