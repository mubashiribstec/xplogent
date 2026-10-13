"""Per-connection plumbing shared by the dashboard WebSockets.

One :class:`WsChannel` per socket owns everything that must die with it:

* ``send`` never raises — once the client is gone it becomes a no-op instead of
  throwing into agent code mid-run;
* approval prompts wait on futures that are **denied** when the socket closes, so a
  run never sits for the full timeout waiting on a browser tab that no longer exists;
* tasks started for the socket are tracked and cancelled on close, so an agent run
  doesn't keep executing tools after its user disconnected.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Coroutine
from typing import Any

from xplogent.safety.approval import ApprovalRequest


class WsChannel:
    def __init__(self, websocket: Any, approval_timeout: float = 300.0) -> None:
        self.ws = websocket
        self.approval_timeout = approval_timeout
        self.closed = False
        self.pending: dict[str, asyncio.Future] = {}
        self.tasks: set[asyncio.Task] = set()

    async def send(self, payload: dict) -> bool:
        if self.closed:
            return False
        try:
            await self.ws.send_json(payload)
            return True
        except Exception:  # noqa: BLE001 - disconnected / closing socket
            self.closed = True
            return False

    async def approve(self, req: ApprovalRequest) -> bool:
        """Ask the browser to approve a tool call; False on timeout or disconnect."""
        if self.closed:
            return False
        approval_id = uuid.uuid4().hex[:8]
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[approval_id] = fut
        try:
            sent = await self.send({
                "type": "approval_required", "id": approval_id,
                "tool": req.tool, "risk": req.risk.value,
                "reason": req.reason, "arguments": req.arguments,
            })
            if not sent:
                return False
            return bool(await asyncio.wait_for(fut, timeout=self.approval_timeout))
        except TimeoutError:
            return False
        finally:
            self.pending.pop(approval_id, None)

    def resolve(self, approval_id: str, allowed: bool) -> None:
        fut = self.pending.pop(approval_id, None)
        if fut and not fut.done():
            fut.set_result(bool(allowed))

    def spawn(self, coro: Coroutine) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def busy(self) -> bool:
        return any(not t.done() for t in self.tasks)

    async def close(self) -> None:
        """Deny outstanding approvals and stop this socket's tasks."""
        self.closed = True
        for fut in list(self.pending.values()):
            if not fut.done():
                fut.set_result(False)
        self.pending.clear()
        tasks = [t for t in self.tasks if not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
