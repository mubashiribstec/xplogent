"""Audit P1: RTP from an unrelated source can't inject audio/DTMF or steer the agent's audio."""

from __future__ import annotations

import struct

from xplogent.voip.telephony.rtp import RtpStream
from xplogent.voip.telephony.sip_proto import PCMU


def pkt(seq=1, pt=PCMU, payload=b"\xff" * 160):
    return struct.pack("!BBHII", 0x80, pt, seq, seq * 160, 1234) + payload


def dtmf(key=5, ts=4000):
    return struct.pack("!BBHII", 0x80, 101, 9, ts, 1234) + bytes([key, 0x80, 0, 160])


def stream(policy="dialog", sdp=("203.0.113.10", 40000), peer="198.51.100.5"):
    s = RtpStream(PCMU, dtmf_pt=101)
    s.policy = policy
    s.remote = sdp
    s.allowed_hosts = {sdp[0], peer}
    return s


def test_first_packet_from_a_stranger_cannot_hijack_the_media():
    s = stream()
    s.datagram_received(pkt(), ("192.0.2.66", 5555))              # attacker first
    assert s.remote == ("203.0.113.10", 40000) and not s.latched   # our audio still goes to the SDP
    assert s.received == 0 and s.rejected == 1 and s._inbox.empty()
    s.datagram_received(pkt(2), ("203.0.113.10", 40000))
    assert s.latched and s.received == 1


def test_nat_peer_on_another_port_is_learnt_but_only_once():
    s = stream()
    s.datagram_received(pkt(), ("198.51.100.5", 61000))           # the PBX's public side (NAT)
    assert s.remote == ("198.51.100.5", 61000) and s.received == 1
    s.datagram_received(pkt(2), ("198.51.100.5", 62000))          # a different port mid-call
    s.datagram_received(pkt(3), ("192.0.2.66", 5555))             # a stranger
    assert s.received == 1 and s.rejected == 2 and s.remote == ("198.51.100.5", 61000)


def test_wrong_source_dtmf_is_ignored():
    s = stream()
    s.datagram_received(pkt(), ("203.0.113.10", 40000))
    got = []
    s._inbox.put_nowait = lambda item: got.append(item)
    s.datagram_received(dtmf(), ("192.0.2.66", 5555))
    assert got == [] and s.dtmf_in == 0
    s.datagram_received(dtmf(), ("203.0.113.10", 40000))
    assert s.dtmf_in == 1


def test_renegotiation_allows_a_new_source():
    s = stream()
    s.datagram_received(pkt(), ("203.0.113.10", 40000))
    s.renegotiate("203.0.113.20", 41000)                          # re-INVITE with new SDP
    assert s.remote == ("203.0.113.20", 41000) and not s.latched
    s.datagram_received(pkt(2), ("203.0.113.20", 41000))
    assert s.latched and s.received == 2


def test_strict_sdp_and_legacy_any():
    s = stream(policy="sdp")
    s.datagram_received(pkt(), ("198.51.100.5", 61000))           # even the SIP peer, wrong port
    assert s.received == 0
    s.datagram_received(pkt(2), ("203.0.113.10", 40000))
    assert s.received == 1
    old = stream(policy="any")
    old.datagram_received(pkt(), ("192.0.2.66", 5555))
    assert old.received == 1 and old.remote == ("192.0.2.66", 5555)
    assert "rejected_wrong_source" in stream().stats()


def test_settings_default_to_dialog():
    from xplogent.voip.telephony.sip import SipSettings

    assert SipSettings.from_config({"sip": {}}).rtp_source == "dialog"
    assert SipSettings.from_config({"sip": {"rtp_source": "any"}}).rtp_source == "any"
    assert SipSettings.from_config({"sip": {"rtp_source": "nonsense"}}).rtp_source == "dialog"
