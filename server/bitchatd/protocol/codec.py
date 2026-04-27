# ─── PROTOCOL CONTRACT ────────────────────────────────────────────────────────
# Derived from: app/src/main/java/com/bitchat/android/protocol/BinaryProtocol.kt
# Last verified against upstream commit: 5b0a7d0 (2026-03-26)
# ──────────────────────────────────────────────────────────────────────────────
from __future__ import annotations
import struct
import time
import zlib
from typing import Optional

from .packet import BitchatPacket
from .constants import (
    PacketFlags, PEER_ID_SIZE, BROADCAST_ID,
    HEADER_V1_SIZE, HEADER_V2_SIZE,
    PADDING_BLOCK_SIZES,
    COMPRESSION_THRESHOLD_BYTES, MAX_PAYLOAD_LENGTH, MAX_COMPRESSION_RATIO,
)


# ── Padding ────────────────────────────────────────────────────────────────────

def _pad(data: bytes) -> bytes:
    """
    PKCS#7-style padding to the next standard block size (256/512/1024/2048).

    Only pads when pad_len fits in one byte (1-255, as PKCS#7 requires).
    If data already fills a block exactly, or pad_len > 255, returns data unchanged
    (the decoder handles unpadded data via its two-pass decode attempt).
    """
    n = len(data)
    for block in PADDING_BLOCK_SIZES:
        if n == block:
            return data          # already block-aligned
        if n < block:
            pad_len = block - n
            if pad_len <= 255:
                return data + bytes([pad_len] * pad_len)
            # pad_len > 255: can't represent in one byte, try next block
    return data


def _unpad(data: bytes) -> bytes:
    """Strip PKCS#7 padding. Returns data unchanged if padding is invalid."""
    if not data:
        return data
    pad_len = data[-1]
    if pad_len == 0 or pad_len > len(data):
        return data
    # validate: all padding bytes must equal pad_len
    if data[-pad_len:] != bytes([pad_len] * pad_len):
        return data
    return data[:-pad_len]


# ── Encode ─────────────────────────────────────────────────────────────────────

def encode(packet: BitchatPacket, pad: bool = True) -> Optional[bytes]:
    """
    Encode a BitchatPacket to bytes (padded by default).

    Returns None on encoding failure.
    pad=False returns unpadded bytes (used internally by FragmentManager).
    """
    try:
        payload = packet.payload

        # Use v2 if the packet has a route
        has_route = bool(packet.route)
        version = 2 if has_route else packet.version

        # Compress payload if it exceeds threshold and compression helps.
        # Prefix format matches Android BinaryProtocol.kt:
        #   v1 → 2-byte uint16 original size (BIG_ENDIAN)
        #   v2 → 4-byte uint32 original size (BIG_ENDIAN)
        compressed = False
        if len(payload) > COMPRESSION_THRESHOLD_BYTES:
            c = zlib.compress(payload, level=6)
            if len(c) < len(payload):
                original_len = len(payload)
                prefix = struct.pack(">H", original_len) if version < 2 else struct.pack(">I", original_len)
                payload = prefix + c
                compressed = True

        # Build flags
        flags = 0
        if packet.recipient_id is not None:
            flags |= PacketFlags.HAS_RECIPIENT
        if packet.signature is not None:
            flags |= PacketFlags.HAS_SIGNATURE
        if compressed:
            flags |= PacketFlags.IS_COMPRESSED
        if has_route:
            flags |= PacketFlags.HAS_ROUTE

        # Timestamp: use current time if not set
        timestamp = packet.timestamp if packet.timestamp else int(time.time() * 1000)

        # Build header — payload_len now covers the full payload blob
        payload_len = len(payload)
        if version >= 2:
            # v2: 16-byte header, 4-byte payload length (uint32)
            header = struct.pack(
                ">BBBQBI",
                version, packet.type, packet.ttl,
                timestamp, flags, payload_len,
            )
        else:
            # v1: 14-byte header, 2-byte payload length (uint16)
            if payload_len > 0xFFFF:
                return None
            header = struct.pack(
                ">BBBQBH",
                version, packet.type, packet.ttl,
                timestamp, flags, payload_len,
            )

        # Build body
        body = bytearray()
        body += _pad_peer_id(packet.sender_id)

        if packet.recipient_id is not None:
            body += _pad_peer_id(packet.recipient_id)

        if has_route and packet.route:
            route = packet.route[:255]
            body += bytes([len(route)])
            for hop in route:
                body += _pad_peer_id(hop)

        body += payload

        if packet.signature is not None:
            body += packet.signature[:64]

        raw = header + bytes(body)
        return _pad(raw) if pad else raw

    except Exception:
        return None


def _pad_peer_id(peer_id: bytes) -> bytes:
    """Zero-pad or truncate a peer ID to exactly PEER_ID_SIZE bytes."""
    if len(peer_id) >= PEER_ID_SIZE:
        return peer_id[:PEER_ID_SIZE]
    return peer_id + bytes(PEER_ID_SIZE - len(peer_id))


# ── Decode ─────────────────────────────────────────────────────────────────────

def _try_decompress(data: bytes) -> Optional[bytes]:
    """Try zlib, raw-deflate, and gzip decompression — Android may use any of these."""
    for wbits in (15, -15, 47):
        try:
            return zlib.decompress(data, wbits=wbits)
        except zlib.error:
            continue
    return None


def decode(data: bytes) -> Optional[BitchatPacket]:
    """
    Decode bytes into a BitchatPacket.

    Tries raw data first, then with padding stripped (matches Android behaviour).
    Returns None if decoding fails.
    """
    pkt = _decode_raw(data)
    if pkt is None:
        pkt = _decode_raw(_unpad(data))
    return pkt


def _decode_raw(data: bytes) -> Optional[BitchatPacket]:
    try:
        # Need at least header + sender ID
        if len(data) < HEADER_V1_SIZE + PEER_ID_SIZE:
            return None

        version = data[0]
        if version not in (1, 2):
            return None

        pkt_type = data[1]
        ttl = data[2]
        timestamp = struct.unpack_from(">Q", data, 3)[0]
        flags = data[11]

        if version >= 2:
            if len(data) < HEADER_V2_SIZE + PEER_ID_SIZE:
                return None
            payload_len = struct.unpack_from(">I", data, 12)[0]
            offset = HEADER_V2_SIZE
        else:
            payload_len = struct.unpack_from(">H", data, 12)[0]
            offset = HEADER_V1_SIZE

        if payload_len > MAX_PAYLOAD_LENGTH:
            return None

        # Sender ID
        if offset + PEER_ID_SIZE > len(data):
            return None
        sender_id = data[offset:offset + PEER_ID_SIZE]
        offset += PEER_ID_SIZE

        # Recipient ID
        recipient_id = None
        if flags & PacketFlags.HAS_RECIPIENT:
            if offset + PEER_ID_SIZE > len(data):
                return None
            recipient_id = data[offset:offset + PEER_ID_SIZE]
            offset += PEER_ID_SIZE

        # Route (v2 only)
        route = None
        if (flags & PacketFlags.HAS_ROUTE) and version >= 2:
            if offset + 1 > len(data):
                return None
            hop_count = data[offset]
            offset += 1
            route = []
            for _ in range(hop_count):
                if offset + PEER_ID_SIZE > len(data):
                    return None
                route.append(bytes(data[offset:offset + PEER_ID_SIZE]))
                offset += PEER_ID_SIZE

        # Payload
        if offset + payload_len > len(data):
            return None

        raw_payload = data[offset:offset + payload_len]
        offset += payload_len

        if flags & PacketFlags.IS_COMPRESSED:
            # v1: 2-byte original size; v2: 4-byte — matches Android BinaryProtocol.kt
            if version >= 2:
                if len(raw_payload) < 4:
                    return None
                original_size = struct.unpack_from(">I", raw_payload, 0)[0]
                compressed_data = raw_payload[4:]
            else:
                if len(raw_payload) < 2:
                    return None
                original_size = struct.unpack_from(">H", raw_payload, 0)[0]
                compressed_data = raw_payload[2:]
            decompressed = _try_decompress(compressed_data)
            if decompressed is None:
                return None
            # Compression bomb protection
            if len(compressed_data) > 0 and len(decompressed) / len(compressed_data) > MAX_COMPRESSION_RATIO:
                return None
            payload = decompressed
        else:
            payload = raw_payload

        # Signature
        signature = None
        if flags & PacketFlags.HAS_SIGNATURE:
            if offset + 64 <= len(data):
                signature = bytes(data[offset:offset + 64])

        return BitchatPacket(
            version=version,
            type=pkt_type,
            ttl=ttl,
            timestamp=timestamp,
            flags=flags,
            sender_id=bytes(sender_id),
            recipient_id=bytes(recipient_id) if recipient_id is not None else None,
            payload=bytes(payload),
            signature=signature,
            route=route,
        )

    except Exception:
        return None
