# ─── PROTOCOL CONTRACT ────────────────────────────────────────────────────────
# Derived from: app/src/main/java/com/bitchat/android/protocol/BinaryProtocol.kt
# Last verified against upstream commit: 5b0a7d0 (2026-03-26)
# ──────────────────────────────────────────────────────────────────────────────
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
from .constants import BROADCAST_ID, MessageType, MESSAGE_TTL_HOPS


@dataclass
class BitchatPacket:
    type: int                          # MessageType constant
    sender_id: bytes                   # 8 bytes
    payload: bytes

    version: int             = 1
    ttl: int                 = MESSAGE_TTL_HOPS
    timestamp: int           = 0       # ms since epoch; 0 = fill at encode time
    flags: int               = 0       # set automatically by codec; do not set manually
    recipient_id: Optional[bytes] = None   # 8 bytes or None (None = broadcast)
    signature: Optional[bytes]    = None   # 64 bytes or None
    route: Optional[list[bytes]]  = None   # list of 8-byte peer IDs (v2 only)

    @property
    def is_broadcast(self) -> bool:
        return self.recipient_id is None or self.recipient_id == BROADCAST_ID

    def with_ttl(self, ttl: int) -> BitchatPacket:
        """Return a copy with a different TTL (used by relay engine)."""
        from dataclasses import replace
        return replace(self, ttl=ttl)
