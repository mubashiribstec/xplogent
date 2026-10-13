"""TraceRecorder — persist the event stream and compute per-agent metrics.

Subscribes to a run's :class:`EventBus`, writes every event to the store
(tagged by run/agent), and maintains live metrics (steps, tool calls, approx
tokens, status) so the dashboard can show deep, per-agent telemetry.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from xplogent.core.events import EventBus, EventType
from xplogent.memory.store import Store


def _new_metric() -> dict[str, Any]:
    return {"name": "", "role": "", "steps": 0, "tool_calls": 0,
            "approx_tokens": 0, "input_tokens": 0, "output_tokens": 0,
            "status": "idle", "current_tool": None}


class TraceRecorder:
    """Persists a run's events for replay and keeps live per-agent metrics.

    Events are written in batches: everything that queued up while the previous
    batch was being written goes in one transaction, and consecutive TOKEN events
    from the same agent are merged into one row (the text is concatenated, so replay
    shows the same output). Writing and committing every streamed token separately
    was the main write load during runs, and quickly hit the replay row limit.
    """

    def __init__(self, bus: EventBus, store: Store, run_id: str, *,
                 retention_days: float = 30.0) -> None:
        self.bus = bus
        self.store = store
        self.run_id = run_id
        self.retention_days = retention_days
        self.metrics: dict[str, dict[str, Any]] = defaultdict(_new_metric)
        self._queue = bus.subscribe()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        try:
            self.store.prune_events(self.retention_days)
        except Exception:  # noqa: BLE001 - housekeeping must not block a run
            pass
        self._task = asyncio.create_task(self._consume())

    async def _consume(self) -> None:
        done = False
        while not done:
            batch = [await self._queue.get()]
            while not self._queue.empty():
                batch.append(self._queue.get_nowait())
            rows: list[tuple[str, str, str, dict[str, Any]]] = []
            for event in batch:
                if event is None:
                    done = True
                    break
                self._track(event)
                agent_id = str(event.data.get("agent_id", "")) or "_"
                prev = rows[-1] if rows else None
                if (event.type == EventType.TOKEN and prev is not None
                        and prev[2] == str(EventType.TOKEN) and prev[1] == agent_id):
                    prev[3]["text"] = str(prev[3].get("text", "")) + str(event.data.get("text", ""))
                    continue
                data = dict(event.data) if event.type == EventType.TOKEN else event.data
                rows.append((self.run_id, agent_id, str(event.type), data))
            self.store.add_events(rows)

    def _track(self, event) -> None:
        data = event.data
        m = self.metrics[str(data.get("agent_id", "")) or "_"]
        if data.get("agent_name"):
            m["name"] = data["agent_name"]
        if data.get("role"):
            m["role"] = data["role"]
        if event.type == EventType.STEP_START:
            m["steps"] += 1
        elif event.type == EventType.TOOL_CALL:
            m["tool_calls"] += 1
            m["current_tool"] = data.get("tool")
        elif event.type == EventType.TOKEN:
            m["approx_tokens"] += max(1, len(str(data.get("text", ""))) // 4)
        elif event.type == EventType.USAGE:
            # real counts when the provider reports them
            m["input_tokens"] += int(data.get("input_tokens", 0))
            m["output_tokens"] += int(data.get("output_tokens", 0))
        elif event.type == EventType.AGENT_STATUS:
            m["status"] = data.get("status", m["status"])
            m["current_tool"] = data.get("current_tool")
        elif event.type == EventType.RUN_END:
            m["status"] = "cancelled" if data.get("cancelled") else "done"

    def snapshot(self) -> list[dict[str, Any]]:
        return [{"agent_id": aid, **m} for aid, m in self.metrics.items() if aid != "_"]

    async def stop(self) -> None:
        """Write whatever is still queued, then stop."""
        if not self._task:
            return
        self.bus.unsubscribe(self._queue)
        self._queue.put_nowait(None)
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout=5)
        except Exception:  # noqa: BLE001 - incl. timeout: never hang shutdown
            self._task.cancel()
