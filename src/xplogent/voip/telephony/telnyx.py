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

    async def transfer(self, call_control_id: str, to_number: str, client_state: str = "") -> dict:
        """Blind transfer. ``client_state`` comes back on the new leg's webhooks, which
        is how a failed transfer (that leg hangs up unbridged) is recognised."""
        body: dict[str, Any] = {"to": to_number}
        if client_state:
            body["client_state"] = base64.b64encode(client_state.encode()).decode()
        return await self._action(call_control_id, "transfer", body)

    async def send_dtmf(self, call_control_id: str, digits: str) -> dict:
        return await self._action(call_control_id, "send_dtmf", {"digits": digits, "duration_millis": 250})

    async def dial(self, to_number: str, from_number: str, connection_id: str,
                   webhook_url: str = "", answering_machine_detection: str = "") -> dict:
        """Place an outbound call (used by the V3 dialer). ``answering_machine_detection``:
        ``premium`` / ``detect`` asks Telnyx to report a machine (call.machine.* events)."""
        body = {"to": to_number, "from": from_number, "connection_id": connection_id}
        if webhook_url:
            body["webhook_url"] = webhook_url
        if answering_machine_detection:
            body["answering_machine_detection"] = answering_machine_detection
        resp = await self._client.post("/calls", json=body)
        resp.raise_for_status()
        return resp.json()

    async def get_application(self, application_id: str) -> dict:
        """The Voice API (Call Control) application, e.g. to compare its webhook URL."""
        resp = await self._client.get(f"/call_control_applications/{application_id}")
        resp.raise_for_status()
        return resp.json().get("data", {})

    async def balance(self) -> dict:
        """Cheap authenticated call: validates the API key."""
        resp = await self._client.get("/balance")
        resp.raise_for_status()
        return resp.json().get("data", {})

    async def aclose(self) -> None:
        await self._client.aclose()


def parse_webhook(payload: dict[str, Any]) -> dict[str, str]:
    """Normalise a Telnyx webhook: event, call_control_id, from, to, direction, client_state, digit."""
    data = payload.get("data", payload)
    p = data.get("payload", {})
    state = ""
    if p.get("client_state"):
        try:
            state = base64.b64decode(str(p["client_state"])).decode("utf-8", "replace")
        except ValueError:
            state = ""
    return {
        "event": str(data.get("event_type", "")),
        "call_control_id": str(p.get("call_control_id", "")),
        "from": str(p.get("from", "")),
        "to": str(p.get("to", "")),
        "direction": str(p.get("direction", "incoming")),
        "client_state": state,
        "digit": str(p.get("digit", "")),
        "result": str(p.get("result", "")),          # call.machine.*.detection.ended
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
    if event == "dtmf":
        digit = str((msg.get("dtmf") or {}).get("digit", ""))[:1]
        return {"event": "dtmf", "audio": b"", "stream_id": stream_id, "meta": {"digit": digit}}
    if event in ("start", "stop"):
        return {"event": event, "audio": b"", "stream_id": stream_id, "meta": msg.get(event, {})}
    return {"event": "other", "audio": b"", "stream_id": stream_id, "meta": {}}


def media_message(mulaw: bytes) -> str:
    """Outbound audio frame, exactly as Telnyx's stream schema defines it.

    The schema sets ``additionalProperties: false``: extra keys (such as Twilio's
    ``streamSid``) can get the frame rejected, i.e. a silent call.
    """
    return json.dumps({"event": "media", "media": {"payload": base64.b64encode(mulaw).decode()}})


def clear_message() -> str:
    """Stop current playback and drop queued audio (barge-in)."""
    return json.dumps({"event": "clear"})


# ── webhook signatures (Ed25519) ──────────────────────────────────────────────
SIGNATURE_TOLERANCE_S = 300


def verify_signature(public_key_b64: str, signature_b64: str, timestamp: str, body: bytes,
                     now: float | None = None) -> str:
    """Check a Telnyx webhook signature. Returns "" when valid, else the reason.

    Telnyx signs ``"{telnyx-timestamp}|{raw body}"`` with Ed25519; the account's public
    key (base64) is shown in the Telnyx portal (Keys & Credentials → Public Key).
    """
    import time

    if not signature_b64 or not timestamp:
        return "missing signature headers"
    try:
        ts = int(timestamp)
    except ValueError:
        return "bad timestamp"
    if abs((now if now is not None else time.time()) - ts) > SIGNATURE_TOLERANCE_S:
        return "timestamp too old or in the future"
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        return "signature check needs the cryptography package"
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64.strip()))
        key.verify(base64.b64decode(signature_b64), f"{timestamp}|".encode() + body)
    except InvalidSignature:
        return "invalid signature"
    except (ValueError, TypeError):
        return "bad public key or signature encoding"
    return ""
