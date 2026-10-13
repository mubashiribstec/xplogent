"""Expose Xplogent itself as an MCP server.

Any MCP client (Claude Desktop, Claude Code via ``claude mcp add``, or another
agent) can call into Xplogent: delegate whole tasks to a single agent, launch a
multi-agent team, query memory/skills, and — when enabled — drive the individual
PC-control tools, all through Xplogent's safety/rights layer.

The tool *logic* lives in :class:`XplogentMCP` and never imports the ``mcp``
package, so it is unit-testable offline. The SDK is imported lazily only in
:func:`create_mcp_server` / :func:`run_server` (same pattern as the FastAPI
``create_app``).
"""

from __future__ import annotations

from typing import Any

from xplogent.core.config import Config, load_config
from xplogent.core.events import EventBus
from xplogent.core.logging import get_logger
from xplogent.memory.store import Store
from xplogent.providers.base import ToolSpec
from xplogent.safety.approval import ApprovalRequest, RiskLevel, SafetyManager
from xplogent.safety.profile import PermissionProfile
from xplogent.tools.registry import ToolRegistry

_log = get_logger("mcp_server")

# Risk tiers that an `auto_approve` MCP server will clear without a human.
_AUTO_APPROVE_MAX = {RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH}


def _agent_tool_specs() -> list[ToolSpec]:
    """Static, high-level tools that wrap whole Xplogent capabilities."""
    return [
        ToolSpec(
            name="xplogent_run_agent",
            description="Run a single Xplogent agent on a task and return its final answer. "
                        "The agent uses its own tools (shell, files, web, …) under Xplogent's "
                        "safety layer.",
            parameters={
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "What the agent should do."},
                    "role": {"type": "string", "description": "Optional role profile (only roles listed in the server's mcp.server.allowed_roles are honoured)."},
                },
                "required": ["task"],
            },
        ),
        ToolSpec(
            name="xplogent_orchestrate",
            description="Run a multi-agent Xplogent team on a goal. The orchestrator decomposes "
                        "the goal into subtasks and runs role-scoped agents concurrently.",
            parameters={
                "type": "object",
                "properties": {
                    "goal": {"type": "string"},
                    "max_concurrent": {"type": "integer", "description": "Max agents at once (default 3)."},
                },
                "required": ["goal"],
            },
        ),
        ToolSpec(
            name="xplogent_search_memory",
            description="Search Xplogent's persistent memory (facts + past messages).",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        ),
        ToolSpec(
            name="xplogent_list_skills",
            description="List the skills Xplogent has learned.",
            parameters={"type": "object", "properties": {}},
        ),
        ToolSpec(
            name="xplogent_remember",
            description="Store a durable fact in Xplogent's long-term memory.",
            parameters={
                "type": "object",
                "properties": {"fact": {"type": "string"}},
                "required": ["fact"],
            },
        ),
    ]


_AGENT_TOOL_NAMES = {t.name for t in _agent_tool_specs()}


class XplogentMCP:
    """Transport-agnostic Xplogent-over-MCP core (no ``mcp`` dependency)."""

    def __init__(self, config: Config | None = None) -> None:
        self.config = config or load_config()
        server_cfg = self.config.mcp.get("server", {})
        self.role = server_cfg.get("agent_role", "operator")
        self.auto_approve = bool(server_cfg.get("auto_approve", False))
        self.expose_raw_tools = bool(server_cfg.get("expose_raw_tools", True))

        self.profile = PermissionProfile.from_role(self.role, self.config.roles)
        base_tools = ToolRegistry.from_config(self.config.tools.get("enabled"))
        self.tools = base_tools.filtered(self.profile.tool_filter())
        self.safety = SafetyManager.from_config(self.config.safety).with_profile(
            self.profile, self.config.safety
        )

    # -- approval over a non-interactive transport -----------------------------
    def _approver(self):
        if not self.auto_approve:
            return None  # confirm-tier actions are blocked (fail safe)

        async def approve(req: ApprovalRequest) -> bool:
            return req.risk in _AUTO_APPROVE_MAX

        return approve

    # -- catalog ---------------------------------------------------------------
    def tool_specs(self) -> list[ToolSpec]:
        specs = list(_agent_tool_specs())
        if self.expose_raw_tools:
            specs.extend(self.tools.specs())
        return specs

    def resource_uris(self) -> list[tuple[str, str]]:
        return [
            ("xplogent://skills", "Skills Xplogent has learned"),
            ("xplogent://runs", "Recent multi-agent run traces"),
        ]

    # -- dispatch --------------------------------------------------------------
    async def dispatch(self, name: str, arguments: dict[str, Any]) -> str:
        if name in _AGENT_TOOL_NAMES:
            return await self._dispatch_agent_tool(name, arguments)
        if self.expose_raw_tools:
            return await self._dispatch_raw_tool(name, arguments)
        return f"ERROR: unknown tool '{name}'"

    async def _dispatch_agent_tool(self, name: str, arguments: dict[str, Any]) -> str:
        # imported here to avoid a heavy import when only listing tools
        from xplogent.runtime import build_orchestrator, build_runtime

        if name == "xplogent_run_agent":
            # The client may only pick a role the operator explicitly allowed
            # (mcp.server.allowed_roles); otherwise the configured role applies, so a client
            # can't escape a restricted role by asking for "operator".
            asked = str(arguments.get("role") or "")
            allowed = set((self.config.mcp.get("server", {}) or {}).get("allowed_roles") or [])
            role = asked if asked and asked in allowed else self.role
            runtime = build_runtime(self.config, bus=EventBus(),
                                    approve=self._approver(), role=role)
            try:
                return await runtime.agent.run(str(arguments.get("task", "")))
            finally:
                await runtime.aclose()

        if name == "xplogent_orchestrate":
            runtime = build_orchestrator(self.config, bus=EventBus(), approve=self._approver())
            try:
                result = await runtime.orchestrator.run_goal(
                    str(arguments.get("goal", "")),
                    max_concurrent=arguments.get("max_concurrent"),
                )
                return _format_orchestration(result)
            finally:
                await runtime.aclose()

        if name == "xplogent_search_memory":
            return self._search_memory(str(arguments.get("query", "")))
        if name == "xplogent_list_skills":
            return self._list_skills()
        if name == "xplogent_remember":
            return await self._remember(str(arguments.get("fact", "")))
        return f"ERROR: unknown tool '{name}'"

    async def _dispatch_raw_tool(self, name: str, arguments: dict[str, Any]) -> str:
        tool = self.tools.get(name)
        if tool is None:
            return f"ERROR: unknown tool '{name}'"
        decision = await self.safety.evaluate(tool, arguments, self._approver())
        if not decision.allowed:
            return f"BLOCKED by safety policy ({decision.risk.value}): {decision.reason}"
        try:
            result = await tool.run(**arguments)
            return result.as_text()
        except TypeError as exc:
            return f"ERROR: bad arguments for {name}: {exc}"
        except Exception as exc:  # noqa: BLE001
            return f"ERROR running {name}: {exc}"

    # -- memory/skills helpers -------------------------------------------------
    def _search_memory(self, query: str) -> str:
        store = Store(self.config.db_path)
        facts = [f.content for f in store.all_facts() if query.lower() in f.content.lower()]
        msgs = store.search_messages(query, limit=10)
        store.close()
        lines = [f"- {f}" for f in facts] + [f"[{m['role']}] {m['content'][:150]}" for m in msgs]
        return "\n".join(lines) or "(nothing found)"

    def _list_skills(self) -> str:
        store = Store(self.config.db_path)
        skills = store.all_skills()
        store.close()
        if not skills:
            return "(no skills learned yet)"
        return "\n".join(f"{s.name} (used {s.uses}×): {s.description}" for s in skills)

    async def _remember(self, fact: str) -> str:
        from xplogent.memory.manager import MemoryManager
        from xplogent.memory.vector import Embedder
        from xplogent.providers.registry import build_provider

        store = Store(self.config.db_path)
        embed_provider = build_provider(self.config.embedding_model)
        mem = MemoryManager(store, Embedder(embed_provider))
        try:
            await mem.remember(fact, source="mcp")
            return "remembered"
        finally:
            await embed_provider.aclose()
            store.close()

    def read_resource(self, uri: str) -> str:
        if uri.startswith("xplogent://skills"):
            return self._list_skills()
        if uri.startswith("xplogent://runs"):
            store = Store(self.config.db_path)
            runs = store.list_runs()
            store.close()
            return "\n".join(f"{r['id']} [{r['status']}] {r['goal']}" for r in runs) or "(no runs)"
        if uri.startswith("xplogent://memory"):
            q = uri.split("?q=", 1)[1] if "?q=" in uri else ""
            return self._search_memory(q)
        return f"(unknown resource: {uri})"


def _format_orchestration(result: dict) -> str:
    lines = [f"run {result.get('run_id')} · peak concurrency {result.get('peak_concurrency')}"]
    for t in result.get("tasks", []):
        lines.append(f"[{t['status']}] {t['title']} ({t['role']})")
        if t.get("result"):
            lines.append(f"    {t['result'][:500]}")
    return "\n".join(lines)


# ── MCP wiring (imports the `mcp` SDK lazily) ─────────────────────────────────
def create_mcp_server(xpl: XplogentMCP | None = None):
    """Build a low-level ``mcp.server.Server`` exposing Xplogent.

    The low-level Server (not FastMCP) is used because raw tools carry arbitrary,
    pre-built JSON schemas.
    """
    try:
        import mcp.types as types
        from mcp.server import Server
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError(
            "The MCP server needs the 'mcp' extra: pip install 'xplogent[mcp]'"
        ) from exc

    xpl = xpl or XplogentMCP()
    server = Server("xplogent")

    @server.list_tools()
    async def list_tools() -> list:
        return [
            types.Tool(name=s.name, description=s.description, inputSchema=s.parameters)
            for s in xpl.tool_specs()
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict | None) -> list:
        text = await xpl.dispatch(name, arguments or {})
        return [types.TextContent(type="text", text=text)]

    @server.list_resources()
    async def list_resources() -> list:
        return [
            types.Resource(uri=uri, name=uri, description=desc)
            for uri, desc in xpl.resource_uris()
        ]

    @server.read_resource()
    async def read_resource(uri) -> str:
        return xpl.read_resource(str(uri))

    return server


def run_server(
    transport: str | None = None,
    host: str | None = None,
    port: int | None = None,
    role: str | None = None,
    auto_approve: bool | None = None,
) -> None:
    """Run the MCP server over the chosen transport."""
    import anyio

    config = load_config()
    server_cfg = config.mcp.get("server", {})
    transport = transport or server_cfg.get("transport", "stdio")
    host = host or server_cfg.get("host", "127.0.0.1")
    port = int(port or server_cfg.get("port", 8766))
    if role:
        config.mcp.setdefault("server", {})["agent_role"] = role
    if auto_approve is not None:
        config.mcp.setdefault("server", {})["auto_approve"] = auto_approve

    xpl = XplogentMCP(config)
    server = create_mcp_server(xpl)

    if transport == "stdio":
        from mcp.server.stdio import stdio_server

        async def _stdio() -> None:
            async with stdio_server() as (read, write):
                await server.run(read, write, server.create_initialization_options())

        _log.info("xplogent MCP server on stdio (role=%s)", xpl.role)
        anyio.run(_stdio)
    elif transport in ("streamable-http", "sse", "http"):
        _run_http(server, host, port, streamable=transport != "sse")
    else:
        raise ValueError(f"unknown transport '{transport}' (use stdio|streamable-http|sse)")


def _run_http(server, host: str, port: int, streamable: bool = True) -> None:
    """Serve the MCP server over HTTP. The more SDK-version-sensitive path."""
    try:
        import contextlib

        import uvicorn
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
        from starlette.applications import Starlette
        from starlette.routing import Mount
    except ImportError as exc:
        raise RuntimeError(
            "HTTP transport needs 'xplogent[api,mcp]'. Use --transport stdio otherwise."
        ) from exc

    manager = StreamableHTTPSessionManager(app=server)

    from xplogent.core.config import load_config as _lc
    from xplogent.interfaces.api.security import (
        configured_hosts,
        configured_origins,
        host_allowed,
        origin_allowed,
    )

    server_cfg = _lc().raw.get("server", {}) or {}
    hosts, origins = configured_hosts(server_cfg), configured_origins(server_cfg)

    async def handle(scope, receive, send):  # ASGI app
        # No token on this transport, so refuse other websites (Origin) and DNS
        # rebinding (Host) — otherwise any page the user visits could drive the agent.
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        if not host_allowed(headers.get("host"), False, hosts) or not origin_allowed(
                headers.get("origin"), headers.get("host"), scope.get("path", ""), origins):
            from starlette.responses import JSONResponse

            await JSONResponse({"error": "forbidden origin/host"}, status_code=403)(
                scope, receive, send)
            return
        await manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with manager.run():
            yield

    app = Starlette(routes=[Mount("/mcp", app=handle)], lifespan=lifespan)
    _log.info("xplogent MCP server on http://%s:%s/mcp", host, port)
    uvicorn.run(app, host=host, port=port)


def main() -> None:
    """Console-script entry (``xplogent-mcp``): run with config defaults."""
    run_server()


if __name__ == "__main__":
    main()
