"""Chrome extension bridge (real-browser control + monitoring)."""

from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from xplogent.interfaces.api import deps

router = APIRouter()


# ── Chrome extension bridge (real-browser control + monitoring) ──────────


@router.websocket("/ws/extension")
async def ws_extension(websocket: WebSocket) -> None:
    if not deps.ws_authorized(websocket):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    from xplogent.core.extension import get_bridge

    bridge = get_bridge()
    bridge.attach(websocket)
    try:
        while True:
            msg = await websocket.receive_json()
            kind = msg.get("type")
            if kind == "snapshot":
                bridge.update_snapshot(msg.get("tabs"), msg.get("inputs"))
            elif kind == "result":
                bridge.resolve(str(msg.get("id", "")), bool(msg.get("ok")), msg.get("data"))
            elif kind == "ping":
                bridge.mark_seen()
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        bridge.detach(websocket)


@router.get("/extension/status")
async def extension_status() -> dict:
    from xplogent.core.extension import get_bridge
    return get_bridge().status()
