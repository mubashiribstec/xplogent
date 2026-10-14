"""Keypad (DTMF): engine collection, RFC 4733 over RTP, SDP negotiation, Telnyx."""

from __future__ import annotations

import asyncio
import socket
import struct

from tests.conftest import ScriptedProvider
from tests.test_voip import make_session, settle
from xplogent.core.config import save_user_config
from xplogent.providers.base import Message, Role, ToolCall
from xplogent.voip.telephony.rtp import RtpStream
from xplogent.voip.telephony.sip_proto import build_sdp, parse_sdp


class _Recorder(ScriptedProvider):
    def __init__(self, replies):
        super().__init__(replies)
        self.seen: list[str] = []

    async def stream(self, messages, tools=None, **kw):
        self.seen.append(messages[-1].content)
        async for ev in super().stream(messages, tools, **kw):
            yield ev


async def test_digits_are_collected_and_given_to_the_agent():
    prov = _Recorder([Message(role=Role.ASSISTANT, content="Thanks, account found."),
                      Message(role=Role.ASSISTANT, content="Option two it is.")])
    session, sent, cleared, turns, hung = make_session(prov, realtime=False, dtmf_gap_s=0.15)
    for d in "123":
        await session.on_dtmf(d)
    await asyncio.sleep(0.3)
    await settle(session)
    assert prov.seen[0] == "[keypad: 123]"
    assert ("caller", "(keypad: 1 2 3)") in turns and turns[-1] == ("agent", "Thanks, account found.")
    await session.on_dtmf("2")
    await session.on_dtmf("#")                   # '#' ends the entry at once
    await settle(session)
    assert prov.seen[1] == "[keypad: 2]"
    await session.on_dtmf("x")                   # not a key: ignored
    await session.stop()


async def test_quick_keys_repeat_and_transfer():
    moved = []

    async def xfer(reason):
        moved.append(reason)

    session, sent, cleared, turns, hung = make_session(ScriptedProvider([]), realtime=False, transfer=xfer,
                                                       dtmf_keys={"*": "repeat", "0": "transfer"})
    session._record("agent", "Our hours are nine to five.")
    await session.on_dtmf("*")
    await settle(session)
    assert turns[-1] == ("caller", "(keypad: *)") and sent              # repeated aloud
    await session.on_dtmf("0")
    await settle(session)
    assert moved == ["caller pressed 0"] and session.transfer_pending
    await session.stop()


async def test_agent_presses_keys_after_speaking():
    pressed = []

    async def send_dtmf(digits):
        pressed.append(digits)

    replies = [Message(role=Role.ASSISTANT, content="Choosing sales.",
                       tool_calls=[ToolCall(id="k", name="press_keys", arguments={"digits": "2#"})]),
               Message(role=Role.ASSISTANT, content="")]
    session, *_ = make_session(ScriptedProvider(replies), realtime=False, send_dtmf=send_dtmf)
    await session.on_text("press two for sales")
    await settle(session)
    assert pressed == ["2#"]
    await session.stop()


def test_press_keys_tool_only_when_the_line_supports_it():
    from xplogent.voip.brain import CallBrain

    assert "press_keys" not in [t.name for t in CallBrain(ScriptedProvider([])).tools]
    brain = CallBrain(ScriptedProvider([]), can_send_dtmf=True)
    assert "press_keys" in [t.name for t in brain.tools]
    assert brain._run_tool("press_keys", {"digits": "1 2;3"}) == "will press 123 after this sentence"
    assert brain.state.dtmf_out == "123"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_rfc4733_round_trip_between_two_rtp_streams():
    got: list[str] = []

    async def on_dtmf(d):
        got.append(d)

    a, b = RtpStream(0, dtmf_pt=101), RtpStream(0, dtmf_pt=101)
    await a.open(_free_port(), "127.0.0.1")
    await b.open(_free_port(), "127.0.0.1")
    a.remote, b.remote = ("127.0.0.1", b.port), ("127.0.0.1", a.port)
    b.on_dtmf = on_dtmf
    a.start()
    b.start()
    a.queue_dtmf("12#")
    for _ in range(150):
        if len(got) == 3:
            break
        await asyncio.sleep(0.02)
    await a.close()
    await b.close()
    assert got == ["1", "2", "#"]                 # each key once despite 3 end packets


async def test_end_packets_are_deduplicated():
    got = []

    async def on_dtmf(d):
        got.append(d)

    rtp = RtpStream(0, dtmf_pt=96)
    rtp.on_dtmf = on_dtmf
    rtp.start()
    pkt = struct.pack("!BBHII", 0x80, 96, 1, 4000, 9) + struct.pack("!BBH", 5, 0x80 | 10, 1280)
    for _ in range(3):
        rtp.datagram_received(pkt, ("127.0.0.1", 4000))
    rtp.datagram_received(struct.pack("!BBHII", 0x80, 96, 2, 4000, 9) + struct.pack("!BBH", 5, 10, 160),
                          ("127.0.0.1", 4000))       # not an end packet: ignored
    await asyncio.sleep(0.05)
    await rtp.close()
    assert got == ["5"]


def test_sdp_dtmf_negotiation():
    offer = parse_sdp(b"v=0\r\nc=IN IP4 1.2.3.4\r\nm=audio 4000 RTP/AVP 0 96\r\n"
                      b"a=rtpmap:0 PCMU/8000\r\na=rtpmap:96 telephone-event/8000\r\n")
    assert offer["dtmf_pt"] == 96
    answer = parse_sdp(build_sdp("5.6.7.8", 5000, (0,), dtmf_pt=96))
    assert answer["dtmf_pt"] == 96 and answer["payloads"] == [0, 96]
    plain = parse_sdp(build_sdp("5.6.7.8", 5000, (0,), dtmf_pt=None))
    assert plain["dtmf_pt"] is None and plain["payloads"] == [0]


async def test_telnyx_dtmf_webhook_reaches_the_call():
    from xplogent.voip.control import LiveCall
    from xplogent.voip.service import VoipService
    from xplogent.voip.telephony.telnyx import parse_stream_message

    save_user_config({"voip": {"enabled": True}})
    svc = VoipService()
    keys = []

    class _S:
        async def on_dtmf(self, d):
            keys.append(d)

    svc.register(LiveCall(call_id=9, session=_S(), brain=None, ref="cc-9"))
    await svc.handle_webhook({"data": {"event_type": "call.dtmf.received",
                                       "payload": {"call_control_id": "cc-9", "digit": "7"}}})
    await asyncio.sleep(0.01)
    assert keys == ["7"]
    assert parse_stream_message('{"event":"dtmf","dtmf":{"digit":"4"}}')["meta"]["digit"] == "4"
