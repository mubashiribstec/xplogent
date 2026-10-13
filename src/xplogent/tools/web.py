"""Web search and page fetch tools (no API key required).

Search uses DuckDuckGo's HTML endpoint; fetch downloads a URL and strips it to
readable text. Both are best-effort and degrade gracefully on network errors.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

import httpx

from xplogent.safety.approval import RiskLevel
from xplogent.tools.base import Tool, ToolResult

_UA = "Mozilla/5.0 (compatible; XplogentAgent/0.1)"


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._skip = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def _strip_html(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    text = "\n".join(parser.parts)
    return re.sub(r"\n{3,}", "\n\n", text)


class WebSearchTool(Tool):
    name = "web_search"
    description = "Search the web and return the top results (title, snippet, URL)."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "max_results": {"type": "integer", "description": "Default 5."},
        },
        "required": ["query"],
    }
    risk = RiskLevel.LOW

    async def run(self, query: str, max_results: int = 5) -> ToolResult:
        url = "https://html.duckduckgo.com/html/"
        try:
            async with httpx.AsyncClient(timeout=30, headers={"User-Agent": _UA}) as client:
                resp = await client.post(url, data={"q": query})
                resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"Search failed: {exc}")

        results = re.findall(
            r'<a rel="nofollow" class="result__a" href="([^"]+)">(.*?)</a>', resp.text
        )
        snippets = re.findall(r'<a class="result__snippet"[^>]*>(.*?)</a>', resp.text, re.DOTALL)
        lines = []
        for i, (link, title) in enumerate(results[:max_results]):
            snip = _strip_html(snippets[i]) if i < len(snippets) else ""
            lines.append(f"{i+1}. {_strip_html(title)}\n   {snip}\n   {link}")
        if not lines:
            return ToolResult.success("No results found.")
        return ToolResult.success("\n".join(lines))


class WebFetchTool(Tool):
    name = "web_fetch"
    description = "Fetch a URL and return its readable text content."
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "max_chars": {"type": "integer", "description": "Default 8000."},
        },
        "required": ["url"],
    }
    risk = RiskLevel.LOW

    async def run(self, url: str, max_chars: int = 8000) -> ToolResult:
        from xplogent.core.netguard import safe_get

        try:
            # Public addresses only (no localhost / LAN / cloud metadata), every redirect
            # re-checked, body capped — a prompt-injected URL can't reach internal services.
            final, html = await safe_get(url, headers={"User-Agent": _UA})
        except ValueError as exc:
            return ToolResult.failure(f"Fetch refused: {exc}")
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"Fetch failed: {exc}")
        text = _strip_html(html)[: max(1, min(int(max_chars or 8000), 100_000))]
        return ToolResult.success(text, url=final)


def web_tools() -> list[Tool]:
    return [WebSearchTool(), WebFetchTool()]
