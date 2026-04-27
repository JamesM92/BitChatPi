# ─── PROTOCOL CONTRACT ────────────────────────────────────────────────────────
# Derived from: app/src/main/java/com/bitchat/android/mesh/FragmentManager.kt
#           and app/src/main/java/com/bitchat/android/model/FragmentPayload.kt
# Last verified against upstream commit: 5b0a7d0 (2026-03-26)
# ──────────────────────────────────────────────────────────────────────────────
from __future__ import annotations
import asyncio
import logging
import os
import struct
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Optional, Callable, Awaitable

log = logging.getLogger(__name__)

from ..protocol.packet import BitchatPacket
from ..protocol.codec import encode, decode, _unpad
from ..protocol.constants import (
    MessageType,
    FRAGMENT_SIZE_THRESHOLD,
    MAX_FRAGMENT_SIZE,
    FRAGMENT_TIMEOUT_MS,
    FRAGMENT_CLEANUP_MS,
    MAX_FRAGMENTS_PER_ID,
    MAX_FRAGMENT_TOTAL_BYTES,
    MAX_ACTIVE_FRAGMENT_SETS,
    MAX_GLOBAL_FRAGMENT_BYTES,
    FRAGMENT_HEADER_SIZE,
    FRAGMENT_ID_SIZE,
    HEADER_V1_SIZE, HEADER_V2_SIZE,
    PEER_ID_SIZE,
)


# ── Fragment payload layout ────────────────────────────────────────────────────
# Matches FragmentPayload.kt exactly:
#   [0:8]   fragment_id  (8 random bytes)
#   [8:10]  index        (uint16 big-endian, 0-based)
#   [10:12] total        (uint16 big-endian)
#   [12]    original_type (uint8)
#   [13:]   data

def _encode_fragment_payload(frag_id: bytes, index: int, total: int,
                              original_type: int, data: bytes) -> bytes:
    header = struct.pack(">8sHHB", frag_id, index, total, original_type)
    return header + data


def _decode_fragment_payload(payload: bytes) -> Optional[tuple[bytes, int, int, int, bytes]]:
    """Returns (frag_id, index, total, original_type, data) or None."""
    if len(payload) < FRAGMENT_HEADER_SIZE:
        return None
    frag_id = payload[:FRAGMENT_ID_SIZE]
    index, total, original_type = struct.unpack_from(">HHB", payload, FRAGMENT_ID_SIZE)
    data = payload[FRAGMENT_HEADER_SIZE:]
    return frag_id, index, total, original_type, data


# ── Internal state for one incoming fragment set ───────────────────────────────

@dataclass
class _FragmentSet:
    original_type: int
    total: int
    timestamp_ms: float
    fragments: dict[int, bytes] = field(default_factory=dict)
    cumulative_bytes: int = 0


# ── FragmentManager ────────────────────────────────────────────────────────────

class FragmentManager:
    """
    Handles outbound fragmentation and inbound reassembly.
    100% iOS/Android compatible — matches FragmentManager.kt logic.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._sets: dict[str, _FragmentSet] = {}
        self._global_bytes: int = 0
        self.on_reassembled: Optional[Callable[[BitchatPacket], Awaitable[None]]] = None
        self._cleanup_task: Optional[asyncio.Task] = None

    def start(self) -> None:
        try:
            loop = asyncio.get_running_loop()
            self._cleanup_task = loop.create_task(self._cleanup_loop())
        except RuntimeError:
            pass

    def stop(self) -> None:
        if self._cleanup_task:
            self._cleanup_task.cancel()

    def is_receiving(self) -> bool:
        return bool(self._sets)

    def is_new_set(self, frag_id: bytes) -> bool:
        return frag_id.hex() not in self._sets

    # ── Outbound ───────────────────────────────────────────────────────────────

    def create_fragments(self, packet: BitchatPacket) -> list[BitchatPacket]:
        """
        Split a large packet into FRAGMENT packets.
        Returns [packet] unchanged if no fragmentation is needed.
        Matches FragmentManager.createFragments() exactly.
        """
        encoded = encode(packet, pad=True)
        if encoded is None:
            return []

        # Strip padding before measuring (matches iOS fix in FragmentManager.kt)
        full_data = _unpad(encoded)

        if len(full_data) <= FRAGMENT_SIZE_THRESHOLD:
            return [packet]

        frag_id = os.urandom(FRAGMENT_ID_SIZE)

        # Dynamic fragment size calculation (matches FragmentManager.kt)
        has_route = bool(packet.route)
        version = 2 if has_route else packet.version
        header_size = HEADER_V2_SIZE if version >= 2 else HEADER_V1_SIZE
        sender_size = PEER_ID_SIZE
        recipient_size = PEER_ID_SIZE if packet.recipient_id is not None else 0
        route_size = (1 + len(packet.route) * PEER_ID_SIZE) if has_route and packet.route else 0
        padding_buffer = 16
        overhead = header_size + sender_size + recipient_size + route_size + FRAGMENT_HEADER_SIZE + padding_buffer
        max_data = min(512 - overhead, MAX_FRAGMENT_SIZE)

        if max_data <= 0:
            return []

        # Split into chunks
        chunks = [
            full_data[i:i + max_data]
            for i in range(0, len(full_data), max_data)
        ]
        total = len(chunks)

        fragments = []
        for idx, chunk in enumerate(chunks):
            frag_payload = _encode_fragment_payload(
                frag_id, idx, total, packet.type, chunk
            )
            frag_pkt = BitchatPacket(
                version=2 if has_route else 1,
                type=MessageType.FRAGMENT,
                ttl=packet.ttl,
                sender_id=packet.sender_id,
                recipient_id=packet.recipient_id,
                timestamp=packet.timestamp,
                payload=frag_payload,
                route=packet.route,
                signature=None,
            )
            fragments.append(frag_pkt)

        return fragments

    # ── Inbound ────────────────────────────────────────────────────────────────

    def handle_fragment(self, packet: BitchatPacket) -> Optional[BitchatPacket]:
        """
        Process an incoming FRAGMENT packet.
        Returns the reassembled BitchatPacket when all fragments arrive, else None.
        Matches FragmentManager.handleFragment() exactly.
        """
        decoded = _decode_fragment_payload(packet.payload)
        if decoded is None:
            return None

        frag_id, index, total, original_type, data = decoded

        if total > MAX_FRAGMENTS_PER_ID:
            return None
        if not data:
            return None

        frag_key = frag_id.hex()

        with self._lock:
            # Validate consistency if set already exists
            if frag_key in self._sets:
                s = self._sets[frag_key]
                if s.total != total or s.original_type != original_type:
                    self._remove_set(frag_key)
                    return None
            else:
                # Safety: cap active sets
                if len(self._sets) >= MAX_ACTIVE_FRAGMENT_SETS:
                    return None
                self._sets[frag_key] = _FragmentSet(
                    original_type=original_type,
                    total=total,
                    timestamp_ms=time.time() * 1000,
                )
                log.info("FRAGMENT new set from %s  id=%s  total=%d  orig_type=0x%02x",
                         packet.sender_id.hex(), frag_key[:8], total, original_type)

            s = self._sets[frag_key]

            # Per-set size cap
            old_size = len(s.fragments.get(index, b""))
            new_set_size = s.cumulative_bytes - old_size + len(data)
            if new_set_size > MAX_FRAGMENT_TOTAL_BYTES:
                self._remove_set(frag_key)
                return None

            # Global size cap
            delta = len(data) - old_size
            if self._global_bytes + delta > MAX_GLOBAL_FRAGMENT_BYTES:
                if frag_key not in s.fragments:
                    self._remove_set(frag_key)
                return None

            s.fragments[index] = data
            s.cumulative_bytes = new_set_size
            self._global_bytes = max(0, self._global_bytes + delta)

            # Check if complete
            if len(s.fragments) == total:
                reassembled = b"".join(s.fragments[i] for i in range(total))
                self._remove_set(frag_key)

                original = decode(reassembled)
                if original is not None:
                    # TTL=0 to suppress re-relay (matches FragmentManager.kt)
                    return original.with_ttl(0)

        return None

    # ── Cleanup ────────────────────────────────────────────────────────────────

    def _remove_set(self, frag_key: str) -> None:
        """Must be called with self._lock held."""
        s = self._sets.pop(frag_key, None)
        if s:
            self._global_bytes = max(0, self._global_bytes - s.cumulative_bytes)

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(FRAGMENT_CLEANUP_MS / 1000)
            self._cleanup_old()

    def _cleanup_old(self) -> None:
        cutoff = time.time() * 1000 - FRAGMENT_TIMEOUT_MS
        with self._lock:
            expired = [k for k, s in self._sets.items() if s.timestamp_ms < cutoff]
            for k in expired:
                s = self._sets[k]
                log.warning("FRAGMENT set expired  id=%s  received=%d/%d  orig_type=0x%02x",
                            k[:8], len(s.fragments), s.total, s.original_type)
                self._remove_set(k)
