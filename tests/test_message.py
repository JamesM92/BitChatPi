"""BitchatMessage binary serialisation round-trip tests."""
import pytest
from bitchatd.messages.message import BitchatMessage, encode_message, decode_message


def roundtrip(msg: BitchatMessage) -> BitchatMessage:
    data = encode_message(msg)
    assert data is not None, "encode_message returned None"
    result = decode_message(data)
    assert result is not None, "decode_message returned None"
    return result


# ── Minimal message ────────────────────────────────────────────────────────────

def test_minimal_round_trip():
    msg = BitchatMessage(sender="Alice", content="hello")
    r = roundtrip(msg)
    assert r.sender == "Alice"
    assert r.content == "hello"
    assert r.id == msg.id
    assert r.timestamp == msg.timestamp
    assert not r.is_relay
    assert not r.is_private


# ── All optional fields ────────────────────────────────────────────────────────

def test_all_optional_fields():
    msg = BitchatMessage(
        sender="Bob",
        content="hi there",
        is_relay=True,
        is_private=True,
        original_sender="Carol",
        recipient_nickname="Dave",
        sender_peer_id="deadbeef01020304",
        mentions=["@Eve", "@Frank"],
        channel="gcpvj",
    )
    r = roundtrip(msg)
    assert r.is_relay
    assert r.is_private
    assert r.original_sender == "Carol"
    assert r.recipient_nickname == "Dave"
    assert r.sender_peer_id == "deadbeef01020304"
    assert r.mentions == ["@Eve", "@Frank"]
    assert r.channel == "gcpvj"


# ── Encrypted content ─────────────────────────────────────────────────────────

def test_encrypted_content():
    msg = BitchatMessage(
        sender="Alice",
        content="",
        is_encrypted=True,
        encrypted_content=b"\x00\x01\x02\x03" * 16,
    )
    r = roundtrip(msg)
    assert r.is_encrypted
    assert r.encrypted_content == msg.encrypted_content
    assert r.content == ""


# ── Unicode content ────────────────────────────────────────────────────────────

def test_unicode_round_trip():
    msg = BitchatMessage(sender="用户", content="こんにちは 🌍")
    r = roundtrip(msg)
    assert r.sender == "用户"
    assert r.content == "こんにちは 🌍"


# ── Long content (up to 65535 bytes) ─────────────────────────────────────────

def test_long_content():
    content = "x" * 60_000
    msg = BitchatMessage(sender="A", content=content)
    r = roundtrip(msg)
    assert r.content == content


# ── Reject short data ─────────────────────────────────────────────────────────

def test_decode_too_short():
    assert decode_message(b"\x00" * 5) is None


def test_decode_empty():
    assert decode_message(b"") is None
