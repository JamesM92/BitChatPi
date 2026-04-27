"""Fragment split + reassembly tests — verifies safety limits from AppConstants."""
import os
import pytest
from bitchatd.protocol.packet import BitchatPacket
from bitchatd.protocol.constants import (
    MessageType, FRAGMENT_SIZE_THRESHOLD, MAX_FRAGMENT_SIZE,
    MAX_FRAGMENTS_PER_ID, MAX_FRAGMENT_TOTAL_BYTES,
    MAX_ACTIVE_FRAGMENT_SETS, MAX_GLOBAL_FRAGMENT_BYTES,
    FRAGMENT_HEADER_SIZE, FRAGMENT_ID_SIZE,
)
from bitchatd.mesh.fragment_manager import (
    FragmentManager, _encode_fragment_payload, _decode_fragment_payload,
)

SENDER = bytes.fromhex("0102030405060708")


def make_manager() -> FragmentManager:
    return FragmentManager()


# ── Fragment payload codec ─────────────────────────────────────────────────────

def test_fragment_payload_round_trip():
    frag_id = bytes(range(FRAGMENT_ID_SIZE))
    data = b"hello"
    encoded = _encode_fragment_payload(frag_id, 2, 5, MessageType.MESSAGE, data)
    assert len(encoded) == FRAGMENT_HEADER_SIZE + len(data)

    result = _decode_fragment_payload(encoded)
    assert result is not None
    fid, idx, total, otype, fdata = result
    assert fid == frag_id
    assert idx == 2
    assert total == 5
    assert otype == MessageType.MESSAGE
    assert fdata == data


def test_fragment_payload_too_short():
    assert _decode_fragment_payload(b"\x00" * (FRAGMENT_HEADER_SIZE - 1)) is None


# ── Small packet: no fragmentation ────────────────────────────────────────────

def test_small_packet_not_fragmented():
    pkt = BitchatPacket(type=MessageType.MESSAGE, sender_id=SENDER, payload=b"hi")
    mgr = make_manager()
    frags = mgr.create_fragments(pkt)
    assert len(frags) == 1
    assert frags[0] is pkt


# ── Large packet: fragmentation + reassembly ──────────────────────────────────

def test_large_packet_fragments_and_reassembles():
    # Use incompressible random bytes so the encoded size reliably exceeds 512
    payload = os.urandom(600)
    pkt = BitchatPacket(
        type=MessageType.MESSAGE,
        sender_id=SENDER,
        payload=payload,
        timestamp=1_700_000_000_000,
    )
    mgr = make_manager()
    frags = mgr.create_fragments(pkt)
    assert len(frags) > 1

    # All fragments must be FRAGMENT type
    for f in frags:
        assert f.type == MessageType.FRAGMENT

    # Feed all fragments to a fresh manager
    mgr2 = make_manager()
    result = None
    for f in frags:
        result = mgr2.handle_fragment(f)
        if result is not None:
            break

    assert result is not None
    assert result.type == MessageType.MESSAGE
    assert result.payload == payload
    # Reassembled packet has TTL=0 (suppresses re-relay)
    assert result.ttl == 0


# ── Partial fragments: no reassembly until complete ───────────────────────────

def test_partial_fragments_return_none():
    payload = os.urandom(600)   # incompressible so fragmentation triggers
    pkt = BitchatPacket(type=MessageType.MESSAGE, sender_id=SENDER, payload=payload)
    mgr = make_manager()
    frags = mgr.create_fragments(pkt)
    assert len(frags) > 1

    mgr2 = make_manager()
    # Feed all but the last
    for f in frags[:-1]:
        assert mgr2.handle_fragment(f) is None


# ── Safety: reject fragment with total > MAX_FRAGMENTS_PER_ID ─────────────────

def test_reject_excessive_total():
    mgr = make_manager()
    frag_id = bytes(FRAGMENT_ID_SIZE)
    payload = _encode_fragment_payload(frag_id, 0, MAX_FRAGMENTS_PER_ID + 1,
                                        MessageType.MESSAGE, b"x")
    pkt = BitchatPacket(type=MessageType.FRAGMENT, sender_id=SENDER, payload=payload)
    assert mgr.handle_fragment(pkt) is None


# ── Safety: reject fragment set exceeding MAX_ACTIVE_FRAGMENT_SETS ─────────────

def test_reject_too_many_active_sets():
    mgr = make_manager()
    # Fill up to the limit with distinct fragment sets (each with total=2, only first piece)
    for i in range(MAX_ACTIVE_FRAGMENT_SETS):
        fid = bytes([i]) + bytes(FRAGMENT_ID_SIZE - 1)
        payload = _encode_fragment_payload(fid, 0, 2, MessageType.MESSAGE, b"x")
        pkt = BitchatPacket(type=MessageType.FRAGMENT, sender_id=SENDER, payload=payload)
        mgr.handle_fragment(pkt)  # fills the set, does not complete it

    # One more distinct set should be rejected
    fid = bytes([MAX_ACTIVE_FRAGMENT_SETS]) + bytes(FRAGMENT_ID_SIZE - 1)
    payload = _encode_fragment_payload(fid, 0, 2, MessageType.MESSAGE, b"x")
    pkt = BitchatPacket(type=MessageType.FRAGMENT, sender_id=SENDER, payload=payload)
    assert mgr.handle_fragment(pkt) is None


# ── Fragment max data size ─────────────────────────────────────────────────────

def test_fragment_data_fits_mtu():
    payload = os.urandom(2000)   # incompressible: guaranteed to fragment
    pkt = BitchatPacket(type=MessageType.MESSAGE, sender_id=SENDER, payload=payload)
    mgr = make_manager()
    frags = mgr.create_fragments(pkt)
    assert len(frags) > 1, "Expected fragmentation for 2000-byte incompressible payload"
    for f in frags:
        parsed = _decode_fragment_payload(f.payload)
        assert parsed is not None
        _, _, _, _, data = parsed
        assert len(data) <= MAX_FRAGMENT_SIZE
