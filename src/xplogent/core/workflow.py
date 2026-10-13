"""Visual workflow execution.

A workflow is a small DAG of nodes the user wires together in the dashboard:

* ``input``  — a literal value that seeds downstream nodes.
* ``agent``  — runs a fresh (memory-free) agent on a prompt.
* ``tool``   — invokes a single registered tool with arguments.

Edges carry outputs forward: a node's prompt/args may reference ``{{input}}``
(all upstream outputs joined) or ``{{<node_id>}}`` (a specific upstream output).
Nodes run in dependency order via a topological sort; independent branches still
run even if a sibling failed. Each node's status/output is emitted as a
``WORKFLOW_NODE`` event so the dashboard can show live progress.
"""

from __future__ import annotations

import re

from xplogent.core.config import Config, load_config
from xplogent.core.events import Event, EventBus, EventType

_VAR = re.compile(r"\{\{([^}]+)\}\}")


def _interp(text: str, outputs: dict[str, str], preds: list[str]) -> str:
    def repl(m: re.Match) -> str:
        key = m.group(1).strip()
        if key == "input":
            return "\n\n".join(outputs.get(p, "") for p in preds if outputs.get(p))
        return str(outputs.get(key, m.group(0)))
    return _VAR.sub(repl, text)


def _topo_order(nodes: dict[str, dict], edges: list[dict]) -> list[str] | None:
    indeg = {nid: 0 for nid in nodes}
    succ: dict[str, list[str]] = {nid: [] for nid in nodes}
    for e in edges:
        a, b = e.get("from"), e.get("to")
        if a in nodes and b in nodes:
            succ[a].append(b)
            indeg[b] += 1
    queue = [nid for nid, d in indeg.items() if d == 0]
    order: list[str] = []
    while queue:
        nid = queue.pop(0)
        order.append(nid)
        for s in succ[nid]:
            indeg[s] -= 1
            if indeg[s] == 0:
                queue.append(s)
    return order if len(order) == len(nodes) else None


async def _emit(bus: EventBus | None, node_id: str, status: str, output: str = "") -> None:
    if bus is not None:
        await bus.publish(Event(EventType.WORKFLOW_NODE,
                                {"node_id": node_id, "status": status, "output": output[:2000]}))


async def _run_node(node: dict, outputs: dict[str, str], preds: list[str],
                    config: Config, bus: EventBus | None, approve=None) -> str:
    ntype = node.get("type")
    cfg = node.get("config", {}) or {}
    if ntype == "input":
        return str(cfg.get("value", ""))
    if ntype == "agent":
        from xplogent.runtime import build_runtime

        prompt = _interp(str(cfg.get("prompt", "")), outputs, preds)
        rt = build_runtime(config, with_memory=False, model=cfg.get("model") or None,
                           approve=approve)
        try:
            return await rt.agent.run(prompt)
        finally:
            await rt.aclose()
    if ntype == "tool":
        from xplogent.tools.registry import ToolRegistry

        tools = ToolRegistry.from_config(config.tools.get("enabled"))
        tool = tools.get(str(cfg.get("tool", "")))
        if tool is None:
            raise ValueError(f"unknown tool '{cfg.get('tool')}'")
        args = {k: (_interp(v, outputs, preds) if isinstance(v, str) else v)
                for k, v in (cfg.get("args") or {}).items()}
        # Same gate as an agent's tool call: upstream outputs flow into these arguments
        # (e.g. a shell command built from an agent's text), so they must not skip it.
        from xplogent.safety.approval import SafetyManager

        decision = await SafetyManager.from_config(config.safety).evaluate(tool, args, approve)
        if not decision.allowed:
            raise PermissionError(f"tool '{tool.name}' blocked ({decision.risk.value}): "
                                  f"{decision.reason or 'needs approval'}")
        result = await tool.run(**args)
        return result.as_text()
    raise ValueError(f"unknown node type '{ntype}'")


async def run_workflow(graph: dict, config: Config | None = None, *,
                       bus: EventBus | None = None, approve=None) -> dict:
    """Execute a workflow DAG and return per-node results.

    ``approve`` answers confirm-tier tool calls; without one they are blocked (fail safe).
    """
    config = config or load_config()
    nodes = {n["id"]: n for n in graph.get("nodes", []) if n.get("id")}
    edges = graph.get("edges", [])
    if not nodes:
        return {"ok": False, "error": "workflow has no nodes", "results": []}

    order = _topo_order(nodes, edges)
    if order is None:
        return {"ok": False, "error": "workflow has a cycle", "results": []}

    preds: dict[str, list[str]] = {nid: [] for nid in nodes}
    for e in edges:
        if e.get("from") in nodes and e.get("to") in nodes:
            preds[e["to"]].append(e["from"])

    outputs: dict[str, str] = {}
    results: list[dict] = []
    for nid in order:
        node = nodes[nid]
        await _emit(bus, nid, "running")
        try:
            out = await _run_node(node, outputs, preds[nid], config, bus, approve)
            outputs[nid] = out
            results.append({"node_id": nid, "name": node.get("name", nid),
                            "status": "done", "output": out})
            await _emit(bus, nid, "done", out)
        except Exception as exc:  # noqa: BLE001 - one node's failure shouldn't abort siblings
            results.append({"node_id": nid, "name": node.get("name", nid),
                            "status": "error", "output": str(exc)})
            await _emit(bus, nid, "error", str(exc))

    return {"ok": True, "results": results, "outputs": outputs}
