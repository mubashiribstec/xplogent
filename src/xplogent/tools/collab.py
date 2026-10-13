"""Collaboration tools — how agents tell each other what they're doing.

Each tool is bound to the calling agent's identity and the shared
:class:`~xplogent.core.messaging.MessageBus`. They are registered only in
multi-agent runs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from xplogent.core.messaging import MessageBus
from xplogent.safety.approval import RiskLevel
from xplogent.tools.base import Tool, ToolResult

# Spawns a sub-agent: (task, role, depth) -> answer.
DelegateCallback = Callable[[str, str, int], Awaitable[str]]


class BackgroundTasks:
    """Shared registry of fire-and-forget subagent tasks for one run.

    A team lead dispatches work with ``delegate_task(background=true)`` and keeps
    going; ``collect_tasks`` later drains finished results. Collected results are
    removed so each is reported once.
    """

    MAX_RUNNING = 8  # cap fan-out so one agent can't launch an unbounded swarm

    def __init__(self) -> None:
        self._tasks: dict[str, dict] = {}
        self._n = 0
        self._running: set[asyncio.Task] = set()  # strong refs: unreferenced tasks get GC'd

    def can_start(self) -> bool:
        return self.pending() < self.MAX_RUNNING

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._running.add(task)
        task.add_done_callback(self._running.discard)
        return task

    async def cancel_all(self) -> None:
        """Stop helpers still running when the run ends (before providers are closed)."""
        for task in list(self._running):
            task.cancel()
        if self._running:
            await asyncio.gather(*self._running, return_exceptions=True)

    def register(self, desc: str) -> str:
        self._n += 1
        tid = f"bg{self._n}"
        self._tasks[tid] = {"status": "running", "result": None, "task": desc}
        return tid

    def complete(self, tid: str, result: str, ok: bool = True) -> None:
        if tid in self._tasks:
            self._tasks[tid].update(status="done" if ok else "error", result=result)

    def pending(self) -> int:
        return sum(1 for v in self._tasks.values() if v["status"] == "running")

    def collect(self) -> list[dict]:
        done = [{"id": k, **v} for k, v in self._tasks.items() if v["status"] != "running"]
        for d in done:
            self._tasks.pop(d["id"], None)
        return done


class BroadcastTool(Tool):
    name = "broadcast"
    description = (
        "Announce what you are doing (or what you found) to all other agents on "
        "the team. Use this to keep everyone in sync."
    )
    parameters = {
        "type": "object",
        "properties": {"content": {"type": "string", "description": "Status update to share."}},
        "required": ["content"],
    }
    risk = RiskLevel.LOW

    def __init__(self, bus: MessageBus, agent_id: str, agent_name: str) -> None:
        self._bus, self._id, self._name = bus, agent_id, agent_name

    async def run(self, content: str) -> ToolResult:
        await self._bus.broadcast(self._id, self._name, content)
        return ToolResult.success("broadcast sent to the team")


class SendMessageTool(Tool):
    name = "send_message"
    description = "Send a direct message to one specific agent (by name)."
    parameters = {
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "Recipient agent name."},
            "content": {"type": "string"},
        },
        "required": ["to", "content"],
    }
    risk = RiskLevel.LOW

    def __init__(self, bus: MessageBus, agent_id: str, agent_name: str) -> None:
        self._bus, self._id, self._name = bus, agent_id, agent_name

    async def run(self, to: str, content: str) -> ToolResult:
        result = await self._bus.send(self._id, self._name, to, content)
        return ToolResult.success(result)


class ReadInboxTool(Tool):
    name = "read_inbox"
    description = "Read and clear messages other agents have sent you since you last checked."
    parameters = {"type": "object", "properties": {}}
    risk = RiskLevel.LOW

    def __init__(self, bus: MessageBus, agent_id: str, agent_name: str) -> None:
        self._bus, self._id, self._name = bus, agent_id, agent_name

    async def run(self) -> ToolResult:
        msgs = self._bus.read_inbox(self._id)
        if not msgs:
            return ToolResult.success("(no new messages)")
        lines = [
            f"[{m.sender_name}{'' if m.recipient is None else ' →you'}]: {m.content}"
            for m in msgs
        ]
        return ToolResult.success("\n".join(lines))


class ListAgentsTool(Tool):
    name = "list_agents"
    description = "List the other agents currently on the team and their roles."
    parameters = {"type": "object", "properties": {}}
    risk = RiskLevel.LOW

    def __init__(self, bus: MessageBus, agent_id: str, agent_name: str) -> None:
        self._bus, self._id, self._name = bus, agent_id, agent_name

    async def run(self) -> ToolResult:
        others = [a for a in self._bus.agents() if a["id"] != self._id]
        if not others:
            return ToolResult.success("(you are the only agent)")
        return ToolResult.success("\n".join(f"{a['name']} ({a['role']})" for a in others))


class DelegateTool(Tool):
    name = "delegate_task"
    description = (
        "Spin up a focused helper agent to handle a self-contained subtask and "
        "return its result. Use this to parallelize independent work or hand off a "
        "specialized job (give it a clear, complete instruction)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "The complete instruction for the helper."},
            "role": {"type": "string",
                     "description": "Role profile for the helper (default 'operator')."},
            "background": {"type": "boolean",
                           "description": "Dispatch asynchronously and keep working; gather the "
                                          "result later with collect_tasks. Default false (wait)."},
        },
        "required": ["task"],
    }
    risk = RiskLevel.MEDIUM

    def __init__(self, delegate: DelegateCallback, depth: int, max_depth: int,
                 background_tasks: BackgroundTasks | None = None) -> None:
        self._delegate, self._depth, self._max = delegate, depth, max_depth
        self._bg = background_tasks

    async def run(self, task: str, role: str = "operator", background: bool = False) -> ToolResult:
        if self._depth >= self._max:
            return ToolResult.failure(
                f"delegation depth limit reached ({self._max}); do this work yourself."
            )
        if background and self._bg is not None:
            if not self._bg.can_start():
                return ToolResult.failure(
                    f"{self._bg.MAX_RUNNING} background helpers are already running; "
                    "call collect_tasks first or do this one in the foreground.")
            tid = self._bg.register(task)

            async def _go() -> None:
                try:
                    answer = await self._delegate(task, role, self._depth + 1)
                    self._bg.complete(tid, answer or "(no answer)", ok=True)
                except Exception as exc:  # noqa: BLE001 - background failure mustn't crash the lead
                    self._bg.complete(tid, f"error: {exc}", ok=False)

            self._bg.spawn(_go())
            return ToolResult.success(
                f"dispatched background task {tid}. Keep working; call collect_tasks "
                "to gather results when ready.")
        answer = await self._delegate(task, role, self._depth + 1)
        return ToolResult.success(answer or "(helper returned no answer)")


class CollectTasksTool(Tool):
    name = "collect_tasks"
    description = (
        "Collect results from background subagents you dispatched with "
        "delegate_task(background=true). Returns finished results (once each) and "
        "how many are still running."
    )
    parameters = {"type": "object", "properties": {}}
    risk = RiskLevel.LOW

    def __init__(self, background_tasks: BackgroundTasks) -> None:
        self._bg = background_tasks

    async def run(self) -> ToolResult:
        done = self._bg.collect()
        pending = self._bg.pending()
        if not done:
            return ToolResult.success(f"(no finished background tasks; {pending} still running)")
        lines = [f"[{d['id']} {d['status']}] {d['task'][:50]} -> {str(d['result'])[:400]}"
                 for d in done]
        tail = f"\n({pending} still running)" if pending else ""
        return ToolResult.success("\n".join(lines) + tail)


def collab_tools(
    bus: MessageBus,
    agent_id: str,
    agent_name: str,
    *,
    delegate: DelegateCallback | None = None,
    depth: int = 0,
    max_depth: int = 2,
    background_tasks: BackgroundTasks | None = None,
) -> list[Tool]:
    tools: list[Tool] = [
        BroadcastTool(bus, agent_id, agent_name),
        SendMessageTool(bus, agent_id, agent_name),
        ReadInboxTool(bus, agent_id, agent_name),
        ListAgentsTool(bus, agent_id, agent_name),
    ]
    # Only offer delegation while there's still depth budget left.
    if delegate is not None and depth < max_depth:
        tools.append(DelegateTool(delegate, depth, max_depth, background_tasks))
        if background_tasks is not None:
            tools.append(CollectTasksTool(background_tasks))
    return tools
