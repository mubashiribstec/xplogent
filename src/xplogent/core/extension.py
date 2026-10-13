"""Bridge to the Xplogent Chrome extension.

The extension connects to ``/ws/extension`` and becomes the agent's hands and
eyes in the user's *real* browser: it streams a live snapshot (open tabs + recent
input-field activity) and executes commands (list/open tabs, navigate, read,
click, type) dispatched by the agent's ``web_browser`` tool.

A single process-wide :class:`ExtensionBridge` is shared by the WebSocket
endpoint, the REST status route, and the tool, correlating each command with its
reply via a future keyed by request id.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any


class ExtensionBridge:
    def __init__(self) -> None:
        self._ws: Any = None
        # request id -> (socket the command went to, reply future)
        self._pending: dict[str, tuple[Any, asyncio.Future]] = {}
        self._n = 0
        self.tabs: list[dict] = []
        self.inputs: list[dict] = []      # recent input-field activity (ring buffer)
        self.last_seen: float = 0.0

    @property
    def connected(self) -> bool:
        return self._ws is not None

    def attach(self, ws: Any) -> None:
        self._ws = ws
        self.last_seen = time.time()

    def detach(self, ws: Any) -> None:
        """Forget ``ws``; fail only the commands that were sent to it.

        When the extension reconnects, the new socket attaches before the old one's
        handler finishes — the old socket's detach must not fail commands that are
        waiting on the new one.
        """
        if self._ws is ws:
            self._ws = None
        for rid, (owner, fut) in list(self._pending.items()):
            if owner is ws:
                self._pending.pop(rid, None)
                if not fut.done():
                    fut.set_exception(ConnectionError("extension disconnected"))

    def update_snapshot(self, tabs: list[dict] | None, inputs: list[dict] | None) -> None:
        if tabs is not None:
            self.tabs = tabs
        for item in inputs or []:
            item.setdefault("ts", time.time())
            self.inputs.append(item)
        self.inputs = self.inputs[-50:]
        self.last_seen = time.time()

    def resolve(self, request_id: str, ok: bool, data: Any) -> None:
        entry = self._pending.pop(request_id, None)
        if entry and not entry[1].done():
            entry[1].set_result({"ok": ok, "data": data})

    async def request(self, action: str, params: dict | None = None, timeout: float = 20.0) -> dict:
        if not self.connected:
            raise ConnectionError("no browser extension connected")
        self._n += 1
        rid = f"r{self._n}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        ws = self._ws
        self._pending[rid] = (ws, fut)
        try:
            await ws.send_json({"type": "command", "id": rid, "action": action,
                                      "params": params or {}})
        except Exception as exc:  # noqa: BLE001 - socket died between connect check and send
            self._pending.pop(rid, None)
            return {"ok": False, "data": f"extension send failed: {exc}"}
        try:
            return await asyncio.wait_for(fut, timeout)
        except TimeoutError:
            self._pending.pop(rid, None)
            return {"ok": False, "data": f"extension timed out after {timeout}s"}

    def mark_seen(self) -> None:
        self.last_seen = time.time()

    def status(self) -> dict:
        # Connected but silent for a while → likely a suspended MV3 worker.
        stale = self.connected and (time.time() - self.last_seen > 60)
        return {"connected": self.connected, "stale": stale, "tabs": self.tabs,
                "inputs": self.inputs[-25:], "last_seen": self.last_seen}


_BRIDGE = ExtensionBridge()


def get_bridge() -> ExtensionBridge:
    return _BRIDGE
