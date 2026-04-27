"""ANNOUNCE TLV payload encode/decode tests."""
import pytest
from bitchatd.protocol.announce import encode_announce, decode_announce, AnnouncePayload

NOISE_PUB = bytes(range(32))
SIGN_PUB  = bytes(range(32, 64))


def test_round_trip():
    raw = encode_announce("Alice", NOISE_PUB, SIGN_PUB)
    ann = decode_announce(raw)
    assert ann is not None
    assert ann.nickname  == "Alice"
    assert ann.noise_pub == NOISE_PUB
    assert ann.sign_pub  == SIGN_PUB


def test_nickname_truncated_to_15():
    raw = encode_announce("A" * 20, NOISE_PUB, SIGN_PUB)
    ann = decode_announce(raw)
    assert ann is not None
    assert len(ann.nickname) == 15


def test_utf8_nickname():
    raw = encode_announce("用户名", NOISE_PUB, SIGN_PUB)
    ann = decode_announce(raw)
    assert ann is not None
    assert ann.nickname == "用户名"


def test_decode_real_android_payload():
    # Payload captured from a live Android BitChat phone (anon7472, peer 0e0c8d...)
    raw = bytes.fromhex(
        "0108616e6f6e37343732"                               # 01 08 "anon7472"
        "0220fbad8fb2c5d2131cf2e81690064c7d848fed0c54"
        "38193b3f7438e4174d7a3b6b"                           # 02 32 noise_pub
        "03207f1a4729d91abe358f112064b1a4cef42964a49d"
        "e1e4e9096a91d5bba2668221"                           # 03 32 sign_pub
        "0410c0f9185c7f276799be3e520bb9524c2f"               # 04 16 unknown (iOS ext)
    )
    ann = decode_announce(raw)
    assert ann is not None
    assert ann.nickname  == "anon7472"
    assert len(ann.noise_pub) == 32
    assert len(ann.sign_pub)  == 32


def test_unknown_tlv_tag_skipped():
    # Extra unknown tag after the 3 required fields
    raw = encode_announce("Bob", NOISE_PUB, SIGN_PUB) + bytes([0xFF, 0x02, 0xAA, 0xBB])
    ann = decode_announce(raw)
    assert ann is not None
    assert ann.nickname == "Bob"


def test_missing_field_returns_none():
    # Only nickname — missing noise_pub and sign_pub
    raw = bytes([0x01, 0x05]) + b"Alice"
    assert decode_announce(raw) is None


def test_empty_returns_none():
    assert decode_announce(b"") is None


def test_truncated_value_returns_none():
    # length byte says 32 but only 10 bytes follow
    raw = bytes([0x02, 32]) + bytes(10)
    assert decode_announce(raw) is None
