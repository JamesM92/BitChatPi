"""Wire-level codec round-trip tests."""
import time
import pytest
from bitchatd.protocol.packet import BitchatPacket
from bitchatd.protocol.codec import encode, decode, _pad, _unpad
from bitchatd.protocol.constants import MessageType, BROADCAST_ID, PADDING_BLOCK_SIZES


SENDER = bytes.fromhex("0102030405060708")


# ── Padding ────────────────────────────────────────────────────────────────────

def test_pad_round_trip():
    for n in (1, 100, 255, 256, 511, 512, 513, 1023, 1024, 2047, 2048):
        data = bytes(n)
        padded = _pad(data)
        # Padded to a standard block, OR returned as-is when pad_len > 255
        assert len(padded) in PADDING_BLOCK_SIZES or len(padded) == n
        # Unpadding must always recover the original data
        assert _unpad(padded) == data

def test_unpad_invalid_is_identity():
    data = b"\x00" * 10 + b"\x05"   # last byte 5 but not all padding bytes equal 5
    assert _unpad(data) == data


# ── Broadcast message round-trip ───────────────────────────────────────────────

def test_broadcast_encode_decode():
    pkt = BitchatPacket(
        type=MessageType.MESSAGE,
        sender_id=SENDER,
        payload=b"hello world",
        ttl=7,
        timestamp=1_700_000_000_000,
    )
    raw = encode(pkt)
    assert raw is not None
    # Padded to a standard block size
    assert len(raw) in PADDING_BLOCK_SIZES

    recovered = decode(raw)
    assert recovered is not None
    assert recovered.type == MessageType.MESSAGE
    assert recovered.sender_id == SENDER
    assert recovered.payload == b"hello world"
    assert recovered.ttl == 7
    assert recovered.recipient_id is None


# ── Private message (with recipient) ──────────────────────────────────────────

def test_private_encode_decode():
    recipient = bytes.fromhex("aabbccddeeff0011")
    pkt = BitchatPacket(
        type=MessageType.MESSAGE,
        sender_id=SENDER,
        recipient_id=recipient,
        payload=b"secret",
        ttl=5,
        timestamp=1_700_000_000_001,
    )
    raw = encode(pkt)
    recovered = decode(raw)
    assert recovered is not None
    assert recovered.recipient_id == recipient
    assert recovered.payload == b"secret"


# ── With signature ─────────────────────────────────────────────────────────────

def test_signature_round_trip():
    sig = bytes(range(64))
    pkt = BitchatPacket(
        type=MessageType.ANNOUNCE,
        sender_id=SENDER,
        payload=b"announce",
        signature=sig,
    )
    raw = encode(pkt)
    recovered = decode(raw)
    assert recovered is not None
    assert recovered.signature == sig


# ── Compression ────────────────────────────────────────────────────────────────

def test_compression_round_trip():
    payload = b"AAAA" * 200
    pkt = BitchatPacket(type=MessageType.MESSAGE, sender_id=SENDER, payload=payload)
    raw = encode(pkt)
    recovered = decode(raw)
    assert recovered is not None
    assert recovered.payload == payload


def test_v1_compression_uses_2_byte_prefix():
    """Android BinaryProtocol.kt uses uint16 original-size prefix for v1."""
    import zlib, struct
    from bitchatd.protocol.constants import PacketFlags, HEADER_V1_SIZE, PEER_ID_SIZE
    payload = b"AAAA" * 200
    pkt = BitchatPacket(type=MessageType.MESSAGE, sender_id=SENDER, payload=payload)
    raw = encode(pkt, pad=False)
    assert raw is not None
    # Locate IS_COMPRESSED flag
    flags = raw[11]
    assert flags & PacketFlags.IS_COMPRESSED
    # payload_len field is at bytes 12-13 (uint16 for v1)
    payload_len = struct.unpack_from(">H", raw, 12)[0]
    # Body starts at offset 14 + 8 (sender_id) = 22
    body_start = HEADER_V1_SIZE + PEER_ID_SIZE
    blob = raw[body_start:body_start + payload_len]
    # First 2 bytes are original size (uint16)
    orig_size = struct.unpack_from(">H", blob, 0)[0]
    assert orig_size == len(payload)
    compressed_bytes = blob[2:]
    assert zlib.decompress(compressed_bytes) == payload


# ── All 8 packet types encode cleanly ─────────────────────────────────────────

@pytest.mark.parametrize("msg_type", [
    MessageType.ANNOUNCE, MessageType.MESSAGE, MessageType.LEAVE,
    MessageType.NOISE_HANDSHAKE, MessageType.NOISE_ENCRYPTED,
    MessageType.FRAGMENT, MessageType.REQUEST_SYNC, MessageType.FILE_TRANSFER,
])
def test_all_packet_types(msg_type):
    pkt = BitchatPacket(type=msg_type, sender_id=SENDER, payload=b"test")
    raw = encode(pkt)
    assert raw is not None
    recovered = decode(raw)
    assert recovered is not None
    assert recovered.type == msg_type


# ── v2 packet with route ───────────────────────────────────────────────────────

def test_v2_route_round_trip():
    hop1 = bytes.fromhex("1111111111111111")
    hop2 = bytes.fromhex("2222222222222222")
    pkt = BitchatPacket(
        type=MessageType.MESSAGE,
        sender_id=SENDER,
        payload=b"routed",
        route=[hop1, hop2],
    )
    raw = encode(pkt)
    recovered = decode(raw)
    assert recovered is not None
    assert recovered.version == 2
    assert recovered.route == [hop1, hop2]
    assert recovered.payload == b"routed"


# ── Unpadded data also decodes ─────────────────────────────────────────────────

def test_decode_unpadded():
    from bitchatd.protocol.codec import _unpad
    pkt = BitchatPacket(type=MessageType.MESSAGE, sender_id=SENDER, payload=b"hi")
    raw_padded = encode(pkt, pad=True)
    raw_unpadded = encode(pkt, pad=False)
    assert decode(raw_unpadded) is not None
    assert decode(raw_padded) is not None


# ── Reject garbage ────────────────────────────────────────────────────────────

def test_decode_garbage_returns_none():
    assert decode(b"") is None
    assert decode(b"\x00" * 5) is None
    assert decode(bytes(300)) is None    # version=0 → rejected
    assert decode(b"\x03" + bytes(299)) is None  # version=3 → rejected
