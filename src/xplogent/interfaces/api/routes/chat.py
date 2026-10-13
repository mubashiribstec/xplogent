"""Dashboard chat WebSocket (agent runs, council, approvals) and the computer-use operator."""

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
    cfg = load_config()
    qs_session = websocket.query_params.get("session_id")
    session_id = int(qs_session) if qs_session and qs_session.isdigit() else None
    if session_id is None:
        with Store(cfg.db_path) as s:
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

    async def handle_council(msg: dict) -> None:
        """Ask several models the same question at once, then synthesize."""
        from xplogent.providers.base import Message, Role, StreamKind

        models = [m for m in msg.get("models", []) if m]
        task = msg.get("task", "")
        base = [Message(role=Role.SYSTEM, content="You are a helpful assistant."),
                Message(role=Role.USER, content=task)]
        answers: dict[str, str] = {}

        async def run_one(model: str) -> None:
            provider = deps.build_provider(model)
            parts: list[str] = []
            try:
                async for ev in provider.stream(base):
                    if ev.kind == StreamKind.TOKEN:
                        parts.append(ev.text)
                        await send({"type": "council_token",
                                                   "channel": model, "text": ev.text})
                    elif ev.kind == StreamKind.DONE and ev.message and not parts:
                        parts.append(ev.message.content)
                        await send({"type": "council_token",
                                                   "channel": model,
                                                   "text": ev.message.content})
            except Exception as exc:  # noqa: BLE001 - one model failing must not sink the rest
                await send({"type": "council_token", "channel": model,
                                           "text": f"[error: {exc}]"})
            finally:
                await provider.aclose()
            answers[model] = "".join(parts)
            await send({"type": "council_done", "channel": model})

        await asyncio.gather(*[run_one(m) for m in models])

        synthesis = ""
        if msg.get("synthesize", True) and answers:
            synth_model = msg.get("synth_model") or models[0]
            merged = "\n\n".join(f"[{m}]\n{a}" for m, a in answers.items())
            sp = ("You are given answers from multiple AI models to the same question. "
                  "Produce the single best, most accurate combined answer, noting any "
                  f"important disagreements.\n\nQuestion:\n{task}\n\nAnswers:\n{merged}")
            provider = deps.build_provider(synth_model)
            try:
                async for ev in provider.stream(
                        [Message(role=Role.SYSTEM, content="You synthesize multiple answers."),
                         Message(role=Role.USER, content=sp)]):
                    if ev.kind == StreamKind.TOKEN:
                        synthesis += ev.text
                        await send({"type": "council_token",
                                                   "channel": "synthesis", "text": ev.text})
            finally:
                await provider.aclose()
            await send({"type": "council_done", "channel": "synthesis"})

        # Persist the turn so it appears in history.
        if session_id is not None:
            with Store(cfg.db_path) as store:
                store.add_message(session_id, "user", task)
                store.set_session_title(session_id, task[:60])
                store.add_message(session_id, "assistant",
                                  synthesis or "\n\n".join(f"**{m}**: {a}" for m, a in answers.items()))
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
            elif kind == "task" and len([m for m in msg.get("models", []) if m]) > 1:
                chan.spawn(guarded(handle_council(msg)))
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


# ── Computer-use operator ────────────────────────────────────────────────


@router.websocket("/ws/operator")
async def ws_operator(websocket: WebSocket) -> None:
    if not deps.ws_authorized(websocket):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    bus = EventBus()
    chan = WsChannel(websocket)
    send = chan.send
    runtime_box: dict = {"rt": None}

    async def forward() -> None:
        async for ev in bus.stream():
            await send({"type": ev.type.value, **ev.data})

    forwarder = asyncio.create_task(forward())

    async def handle(msg: dict) -> None:
        from xplogent.core.operator import build_operator

        rt = None
        try:
            rt = build_operator(bus=bus, approve=chan.approve,
                                max_steps=int(msg.get("max_steps", 30)))
            runtime_box["rt"] = rt
            await rt.agent.run(str(msg.get("goal", "")))
        except Exception as exc:  # noqa: BLE001 - always end the run for the UI
            await send({"type": "error", "message": f"operator failed: {exc}"})
        finally:
            runtime_box["rt"] = None
            if rt is not None:
                await rt.aclose()
            await send({"type": "done"})

    try:
        while True:
            msg = await websocket.receive_json()
            kind = msg.get("type")
            if kind == "start":
                # One computer-use run per socket: two would fight over the mouse.
                if chan.busy():
                    await send({"type": "error", "message": "an operator run is already active"})
                else:
                    chan.spawn(handle(msg))
            elif kind == "cancel" and runtime_box["rt"] is not None:
                runtime_box["rt"].agent.cancel()
            elif kind == "approval":
                chan.resolve(str(msg.get("id", "")), bool(msg.get("allowed")))
    except WebSocketDisconnect:
        pass
    finally:
        await chan.close()
        await bus.close()
        forwarder.cancel()
        if runtime_box["rt"] is not None:
            await runtime_box["rt"].aclose()


@router.get("/operator/screen")
async def operator_screen():
    """Serve the most recent screenshot the operator captured (for a live preview)."""
    from fastapi.responses import FileResponse, Response

    from xplogent.core.config import xplogent_home

    shots = sorted(xplogent_home().glob("screenshot_*.png"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if not shots:
        return Response(status_code=204)
    return FileResponse(str(shots[0]), media_type="image/png",
                        headers={"Cache-Control": "no-store"})
