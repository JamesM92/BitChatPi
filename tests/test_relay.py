"""Relay engine: TTL, deduplication, relay probabilities."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from bitchatd.protocol.packet import BitchatPacket
from bitchatd.protocol.constants import MessageType, BROADCAST_ID
from bitchatd.mesh.relay_engine import (
    RelayEngine, _should_relay, _has_duplicate_hops, _packet_key,
)

MY_ID   = bytes.fromhex("0000000000000001")
PEER_ID = bytes.fromhex("0000000000000002")
SENDER  = bytes.fromhex("0000000000000003")


def make_packet(ttl=7, recipient_id=None, route=None, ts=1_700_000_000_000):
    return BitchatPacket(
        type=MessageType.MESSAGE,
        sender_id=SENDER,
        payload=b"test",
        ttl=ttl,
        timestamp=ts,
        recipient_id=recipient_id,
        route=route,
    )


# ── TTL ────────────────────────────────────────────────────────────────────────

def test_ttl_zero_not_relayed():
    engine = RelayEngine(MY_ID)
    broadcast = AsyncMock()
    engine.broadcast_packet = broadcast
    asyncio.get_event_loop().run_until_complete(
        engine.handle_relay(make_packet(ttl=0), PEER_ID.hex())
    )
    broadcast.assert_not_called()


def test_ttl_decremented():
    relayed = []
    async def capture(pkt, _from):
        relayed.append(pkt)

    engine = RelayEngine(MY_ID)
    engine.broadcast_packet = capture
    asyncio.get_event_loop().run_until_complete(
        engine.handle_relay(make_packet(ttl=3), PEER_ID.hex())
    )
    assert len(relayed) == 1
    assert relayed[0].ttl == 2


# ── Own packets ignored ────────────────────────────────────────────────────────

def test_own_packet_not_relayed():
    engine = RelayEngine(MY_ID)
    broadcast = AsyncMock()
    engine.broadcast_packet = broadcast
    asyncio.get_event_loop().run_until_complete(
        engine.handle_relay(make_packet(), MY_ID.hex())   # from ourselves
    )
    broadcast.assert_not_called()


# ── Deduplication ─────────────────────────────────────────────────────────────

def test_duplicate_not_relayed_twice():
    relayed = []
    async def capture(pkt, _from):
        relayed.append(pkt)

    engine = RelayEngine(MY_ID)
    engine.broadcast_packet = capture
    pkt = make_packet()
    loop = asyncio.get_event_loop()
    loop.run_until_complete(engine.handle_relay(pkt, PEER_ID.hex()))
    loop.run_until_complete(engine.handle_relay(pkt, PEER_ID.hex()))
    assert len(relayed) == 1


# ── Relay probabilities ────────────────────────────────────────────────────────

@pytest.mark.parametrize("net_size,expected_prob", [
    (1,   1.0),
    (3,   1.0),
    (10,  1.0),
    (30,  0.85),
    (50,  0.7),
    (100, 0.55),
    (101, 0.4),
])
def test_relay_probability(net_size, expected_prob):
    # TTL=2 (< 4) so probability applies
    pkt = make_packet(ttl=2)
    hits = sum(1 for _ in range(10_000) if _should_relay(pkt, net_size))
    if expected_prob == 1.0:
        assert hits == 10_000
    else:
        # Allow ±3% statistical tolerance
        assert abs(hits / 10_000 - expected_prob) < 0.03


def test_high_ttl_always_relays():
    pkt = make_packet(ttl=4)
    for _ in range(100):
        assert _should_relay(pkt, 1000)  # huge network but TTL ≥ 4


# ── Addressed-to-me detection ─────────────────────────────────────────────────

def test_is_addressed_to_me():
    engine = RelayEngine(MY_ID)
    pkt = make_packet(recipient_id=MY_ID)
    assert engine.is_addressed_to_me(pkt)


def test_broadcast_not_addressed_to_me():
    engine = RelayEngine(MY_ID)
    pkt = make_packet(recipient_id=BROADCAST_ID)
    assert not engine.is_addressed_to_me(pkt)


def test_other_recipient_not_addressed_to_me():
    engine = RelayEngine(MY_ID)
    pkt = make_packet(recipient_id=PEER_ID)
    assert not engine.is_addressed_to_me(pkt)


# ── Duplicate hops ────────────────────────────────────────────────────────────

def test_duplicate_hops_detected():
    hop = bytes(8)
    assert _has_duplicate_hops([hop, hop])


def test_no_duplicate_hops():
    assert not _has_duplicate_hops([bytes([1]*8), bytes([2]*8)])
