"""A tiny async event bus.

The agent loop publishes events; interfaces (CLI, API/WebSocket, voice)
subscribe. This keeps the core decoupled from any particular UI and lets the
same run drive a terminal, a browser dashboard, and a voice client at once.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class EventType(StrEnum):
    # lifecycle
    RUN_START = "run_start"
    RUN_END = "run_end"
    STEP_START = "step_start"
    # model output
    TOKEN = "token"                # streamed assistant token
    MESSAGE = "message"            # a complete assistant message
    THINKING = "thinking"          # reasoning/status text
    # tools
    TOOL_CALL = "tool_call"        # model requested a tool
    TOOL_RESULT = "tool_result"    # tool finished
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_RESOLVED = "approval_resolved"
    # memory / skills
    MEMORY = "memory"
    SKILL = "skill"
    # token accounting / context window
    USAGE = "usage"
    # cost guardrails
    BUDGET = "budget"
    # agent-rendered visual workspace
    CANVAS = "canvas"
    # multi-agent
    AGENT_SPAWN = "agent_spawn"      # a worker started
    AGENT_STATUS = "agent_status"    # status change (running/paused/done/cancelled)
    AGENT_MESSAGE = "agent_message"  # inter-agent broadcast/direct message
    RUN_PROGRESS = "run_progress"    # orchestrator-level progress
    TASK_UPDATE = "task_update"      # task board change
    # misc
    ERROR = "error"


@dataclass
class Event:
    type: EventType
    data: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Event({self.type.value}, {self.data!r})"


class EventBus:
    """Fan-out async pub/sub. Subscribers each get their own queue."""

    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue[Event | None]] = []

    def subscribe(self) -> asyncio.Queue[Event | None]:
        queue: asyncio.Queue[Event | None] = asyncio.Queue()
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event | None]) -> None:
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    async def publish(self, event: Event) -> None:
        for queue in list(self._subscribers):
            await queue.put(event)

    def publish_nowait(self, event: Event) -> None:
        for queue in list(self._subscribers):
            queue.put_nowait(event)

    async def stream(self) -> AsyncIterator[Event]:
        """Convenience iterator over this bus until a ``None`` sentinel."""
        queue = self.subscribe()
        try:
            while True:
                event = await queue.get()
                if event is None:
                    return
                yield event
        finally:
            self.unsubscribe(queue)

    async def close(self) -> None:
        for queue in list(self._subscribers):
            await queue.put(None)
