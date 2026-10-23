"""Dashboard chat WebSocket (agent runs, approvals)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from xplogent.core.config import load_config
from xplogent.core.events import EventBus
from xplogent.interfaces.api import deps
from xplogent.interfaces.api.wschannel import WsChannel
from xplogent.memory.store import Store

router = APIRouter()


@router.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    if not deps.ws_authorized(websocket):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    bus = EventBus()
    chan = WsChannel(websocket)
    send = chan.send
    approve = chan.approve

    # Persistent session: reuse the one the client passes, else create one.
    # An invented/stale id must not silently collect orphan messages that no
    # session list will ever show — fall back to a fresh session instead.
    cfg = load_config()
    qs_session = websocket.query_params.get("session_id")
    session_id = int(qs_session) if qs_session and qs_session.isdigit() else None
    with Store(cfg.db_path) as s:
        if session_id is None or not s.session_exists(session_id):
            session_id = s.create_session(title="chat")
    await send({"type": "session", "id": session_id})

    # One runtime at a time; rebuilt (and history-reloaded) on model switch.
    current = {"model": None, "runtime": None}

    async def get_runtime(model: str | None, gen_params: dict):
        model = model or cfg.model
        if current["runtime"] is None or current["model"] != model:
            if current["runtime"] is not None:
                await current["runtime"].aclose()
            current["runtime"] = deps.build_runtime(
                bus=bus, approve=approve, model=model,
                gen_params=gen_params, session_id=session_id,
            )
            current["model"] = model
        else:
            current["runtime"].agent.gen_params = gen_params
        return current["runtime"]

    async def forward() -> None:
        async for ev in bus.stream():
            await send({"type": ev.type.value, **ev.data})

    forwarder = asyncio.create_task(forward())

    running = {"on": False}

    async def handle_task(msg: dict) -> None:
        running["on"] = True
        try:
            gen_params = {k: msg[k] for k in ("effort", "thinking", "max_tokens", "temperature")
                          if msg.get(k) is not None}
            images = msg.get("images") or None
            model = msg.get("model")
            # Images need a model that can see: route image turns to the configured
            # vision model unless the user explicitly picked one.
            if images and not model:
                vm = (load_config().vision_model or "").strip()
                if vm:
                    model = vm
            runtime = await get_runtime(model, gen_params)
            await runtime.agent.run(msg.get("task", ""), images=images)
        except Exception as exc:  # noqa: BLE001 - never leave the chat stuck
            await send({"type": "error", "message": f"run failed: {exc}"})
        finally:
            running["on"] = False
            await send({"type": "done"})

    async def guarded(coro) -> None:
        """Run a handler so it always ends the turn, even on failure."""
        try:
            await coro
        except Exception as exc:  # noqa: BLE001
            await send({"type": "error", "message": str(exc)})
            await send({"type": "done"})

    async def keepalive() -> None:
        # Ping the client periodically so idle proxies don't drop a long turn.
        while True:
            await asyncio.sleep(20)
            if not await send({"type": "ping"}):
                return

    pinger = asyncio.create_task(keepalive())

    try:
        while True:
            msg = await websocket.receive_json()
            kind = msg.get("type")
            if kind == "steer" or (kind == "task" and running["on"]):
                # Mid-run suggestion: fold it into the active run instead of
                # starting a second one on the shared runtime.
                rt = current.get("runtime")
                if rt is not None:
                    rt.agent.inject(str(msg.get("text") or msg.get("task") or ""))
            elif kind == "task":
                chan.spawn(handle_task(msg))
            elif kind == "cancel":
                rt = current.get("runtime")
                if rt is not None:
                    rt.agent.cancel()
            elif kind == "approval":
                chan.resolve(str(msg.get("id", "")), bool(msg.get("allowed")))
    except WebSocketDisconnect:
        pass
    finally:
        # Stop this socket's runs (denying any pending approval) before the
        # runtime they use is closed underneath them.
        await chan.close()
        pinger.cancel()
        await bus.close()
        forwarder.cancel()
        if current["runtime"] is not None:
            await current["runtime"].aclose()
