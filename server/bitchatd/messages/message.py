# ─── PROTOCOL CONTRACT ────────────────────────────────────────────────────────
# Derived from: app/src/main/java/com/bitchat/android/model/BitchatMessage.kt
# Last verified against upstream commit: 633a506 (2025-09-19)
# ──────────────────────────────────────────────────────────────────────────────
from __future__ import annotations
import struct
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional


# ── Flags ──────────────────────────────────────────────────────────────────────
class MsgFlags:
    IS_RELAY             = 0x01
    IS_PRIVATE           = 0x02
    HAS_ORIGINAL_SENDER  = 0x04
    HAS_RECIPIENT_NICK   = 0x08
    HAS_SENDER_PEER_ID   = 0x10
    HAS_MENTIONS         = 0x20
    HAS_CHANNEL          = 0x40
    IS_ENCRYPTED         = 0x80


# ── Data class ─────────────────────────────────────────────────────────────────

@dataclass
class BitchatMessage:
    sender: str                           # nickname
    content: str

    id: str                    = field(default_factory=lambda: str(uuid.uuid4()).upper())
    timestamp: int             = field(default_factory=lambda: int(time.time() * 1000))
    is_relay: bool             = False
    is_private: bool           = False
    original_sender: Optional[str]  = None
    recipient_nickname: Optional[str] = None
    sender_peer_id: Optional[str]    = None
    mentions: Optional[list[str]]    = None
    channel: Optional[str]           = None
    encrypted_content: Optional[bytes] = None
    is_encrypted: bool         = False


# ── Serialise ──────────────────────────────────────────────────────────────────

def encode_message(msg: BitchatMessage) -> Optional[bytes]:
    """
    Serialise a BitchatMessage to bytes.
    Layout matches BitchatMessage.toBinaryPayload() exactly (big-endian).
    """
    try:
        buf = bytearray()

        flags = 0
        if msg.is_relay:            flags |= MsgFlags.IS_RELAY
        if msg.is_private:          flags |= MsgFlags.IS_PRIVATE
        if msg.original_sender:     flags |= MsgFlags.HAS_ORIGINAL_SENDER
        if msg.recipient_nickname:  flags |= MsgFlags.HAS_RECIPIENT_NICK
        if msg.sender_peer_id:      flags |= MsgFlags.HAS_SENDER_PEER_ID
        if msg.mentions:            flags |= MsgFlags.HAS_MENTIONS
        if msg.channel:             flags |= MsgFlags.HAS_CHANNEL
        if msg.is_encrypted:        flags |= MsgFlags.IS_ENCRYPTED

        buf += struct.pack(">B", flags)
        buf += struct.pack(">q", msg.timestamp)

        _write_u8_str(buf, msg.id)
        _write_u8_str(buf, msg.sender)

        if msg.is_encrypted and msg.encrypted_content:
            _write_u16_bytes(buf, msg.encrypted_content)
        else:
            _write_u16_str(buf, msg.content)

        if msg.original_sender:
            _write_u8_str(buf, msg.original_sender)
        if msg.recipient_nickname:
            _write_u8_str(buf, msg.recipient_nickname)
        if msg.sender_peer_id:
            _write_u8_str(buf, msg.sender_peer_id)

        if msg.mentions:
            entries = msg.mentions[:255]
            buf += struct.pack(">B", len(entries))
            for m in entries:
                _write_u8_str(buf, m)

        if msg.channel:
            _write_u8_str(buf, msg.channel)

        return bytes(buf)
    except Exception:
        return None


def decode_message(data: bytes) -> Optional[BitchatMessage]:
    """
    Deserialise bytes into a BitchatMessage.
    Layout matches BitchatMessage.fromBinaryPayload() exactly (big-endian).
    """
    try:
        if len(data) < 13:
            return None

        offset = 0
        flags = data[offset]; offset += 1
        timestamp = struct.unpack_from(">q", data, offset)[0]; offset += 8

        msg_id, offset = _read_u8_str(data, offset)
        if msg_id is None:
            return None

        sender, offset = _read_u8_str(data, offset)
        if sender is None:
            return None

        if offset + 2 > len(data):
            return None
        content_len = struct.unpack_from(">H", data, offset)[0]; offset += 2
        if offset + content_len > len(data):
            return None

        is_encrypted = bool(flags & MsgFlags.IS_ENCRYPTED)
        if is_encrypted:
            encrypted_content = bytes(data[offset:offset + content_len])
            content = ""
        else:
            content = data[offset:offset + content_len].decode("utf-8")
            encrypted_content = None
        offset += content_len

        original_sender = None
        if (flags & MsgFlags.HAS_ORIGINAL_SENDER) and offset < len(data):
            original_sender, offset = _read_u8_str(data, offset)

        recipient_nickname = None
        if (flags & MsgFlags.HAS_RECIPIENT_NICK) and offset < len(data):
            recipient_nickname, offset = _read_u8_str(data, offset)

        sender_peer_id = None
        if (flags & MsgFlags.HAS_SENDER_PEER_ID) and offset < len(data):
            sender_peer_id, offset = _read_u8_str(data, offset)

        mentions = None
        if (flags & MsgFlags.HAS_MENTIONS) and offset < len(data):
            count = data[offset]; offset += 1
            mentions = []
            for _ in range(count):
                if offset >= len(data):
                    break
                m, offset = _read_u8_str(data, offset)
                if m is not None:
                    mentions.append(m)
            if not mentions:
                mentions = None

        channel = None
        if (flags & MsgFlags.HAS_CHANNEL) and offset < len(data):
            channel, offset = _read_u8_str(data, offset)

        return BitchatMessage(
            id=msg_id,
            sender=sender,
            content=content,
            timestamp=timestamp,
            is_relay=bool(flags & MsgFlags.IS_RELAY),
            is_private=bool(flags & MsgFlags.IS_PRIVATE),
            original_sender=original_sender,
            recipient_nickname=recipient_nickname,
            sender_peer_id=sender_peer_id,
            mentions=mentions,
            channel=channel,
            encrypted_content=encrypted_content,
            is_encrypted=is_encrypted,
        )
    except Exception:
        return None


# ── Helpers ────────────────────────────────────────────────────────────────────

def _write_u8_str(buf: bytearray, s: str) -> None:
    b = s.encode("utf-8")[:255]
    buf += struct.pack(">B", len(b))
    buf += b


def _write_u16_str(buf: bytearray, s: str) -> None:
    b = s.encode("utf-8")[:65535]
    buf += struct.pack(">H", len(b))
    buf += b


def _write_u16_bytes(buf: bytearray, data: bytes) -> None:
    d = data[:65535]
    buf += struct.pack(">H", len(d))
    buf += d


def _read_u8_str(data: bytes, offset: int) -> tuple[Optional[str], int]:
    if offset >= len(data):
        return None, offset
    length = data[offset]; offset += 1
    if offset + length > len(data):
        return None, offset
    s = data[offset:offset + length].decode("utf-8")
    return s, offset + length
