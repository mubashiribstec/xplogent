"""Telnyx adapter: call-control REST actions + media-stream message handling.

Flow for an inbound call:
  1. Telnyx POSTs ``call.initiated`` to our webhook  -> we ``answer``.
  2. On ``call.answered`` we ``streaming_start`` pointing the carrier at our
     ``wss://.../ws/telephony/<token>`` endpoint.
  3. Audio then flows over that WebSocket as JSON messages carrying base64 G.711 μ-law.

The Twilio media-stream wire format is nearly identical (``start`` / ``media`` / ``stop``
events), so :func:`parse_stream_message` accepts both. Telnyx-specific request fields below
(``stream_bidirectional_*``) follow Telnyx's call-control docs and should be confirmed
against your account during the first live test call.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

import httpx

API_BASE = "https://api.telnyx.com/v2"


class TelnyxClient:
    def __init__(self, api_key: str | None = None, api_key_env: str = "TELNYX_API_KEY") -> None:
        key = api_key or os.environ.get(api_key_env, "")
        self._client = httpx.AsyncClient(
            base_url=API_BASE, timeout=httpx.Timeout(15.0),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        self.has_key = bool(key)

    async def _action(self, call_control_id: str, action: str, body: dict[str, Any] | None = None) -> dict:
        resp = await self._client.post(f"/calls/{call_control_id}/actions/{action}", json=body or {})
        resp.raise_for_status()
        return resp.json()

    async def answer(self, call_control_id: str) -> dict:
        return await self._action(call_control_id, "answer")

    async def start_stream(self, call_control_id: str, stream_url: str) -> dict:
        return await self._action(call_control_id, "streaming_start", {
            "stream_url": stream_url,
            "stream_track": "inbound_track",
            "stream_bidirectional_mode": "rtp",
            "stream_bidirectional_codec": "PCMU",
        })

    async def hangup(self, call_control_id: str) -> dict:
        return await self._action(call_control_id, "hangup")

    async def transfer(self, call_control_id: str, to_number: str) -> dict:
        return await self._action(call_control_id, "transfer", {"to": to_number})

    async def dial(self, to_number: str, from_number: str, connection_id: str,
                   webhook_url: str = "") -> dict:
        """Place an outbound call (used by the V3 dialer)."""
        body = {"to": to_number, "from": from_number, "connection_id": connection_id}
        if webhook_url:
            body["webhook_url"] = webhook_url
        resp = await self._client.post("/calls", json=body)
        resp.raise_for_status()
        return resp.json()

    async def aclose(self) -> None:
        await self._client.aclose()


def parse_webhook(payload: dict[str, Any]) -> dict[str, str]:
    """Normalise a Telnyx webhook into ``{event, call_control_id, from, to, direction}``."""
    data = payload.get("data", payload)
    p = data.get("payload", {})
    return {
        "event": str(data.get("event_type", "")),
        "call_control_id": str(p.get("call_control_id", "")),
        "from": str(p.get("from", "")),
        "to": str(p.get("to", "")),
        "direction": str(p.get("direction", "incoming")),
    }


def parse_stream_message(raw: str | bytes) -> dict[str, Any]:
    """Parse one media-stream WebSocket message.

    Returns ``{"event": "start"|"media"|"stop"|"other", "audio": bytes, "stream_id": str,
    "meta": dict}``; ``audio`` is raw μ-law for ``media`` events.
    """
    try:
        msg = json.loads(raw)
    except (ValueError, TypeError):
        return {"event": "other", "audio": b"", "stream_id": "", "meta": {}}
    event = msg.get("event", "")
    stream_id = str(msg.get("stream_id") or msg.get("streamSid")
                    or msg.get("start", {}).get("streamSid", "")
                    or msg.get("start", {}).get("stream_id", ""))
    if event == "media":
        media = msg.get("media", {})
        track = media.get("track", "inbound")
        if track not in ("inbound", "inbound_track"):
            return {"event": "other", "audio": b"", "stream_id": stream_id, "meta": {}}
        try:
            audio = base64.b64decode(media.get("payload", ""))
        except ValueError:
            audio = b""
        return {"event": "media", "audio": audio, "stream_id": stream_id, "meta": {}}
    if event in ("start", "stop"):
        return {"event": event, "audio": b"", "stream_id": stream_id, "meta": msg.get(event, {})}
    return {"event": "other", "audio": b"", "stream_id": stream_id, "meta": {}}


def media_message(mulaw: bytes, stream_id: str = "") -> str:
    """Build an outbound media message (works for Telnyx; Twilio needs ``streamSid``)."""
    msg: dict[str, Any] = {"event": "media", "media": {"payload": base64.b64encode(mulaw).decode()}}
    if stream_id:
        msg["streamSid"] = stream_id
    return json.dumps(msg)


def clear_message(stream_id: str = "") -> str:
    msg: dict[str, Any] = {"event": "clear"}
    if stream_id:
        msg["streamSid"] = stream_id
    return json.dumps(msg)
