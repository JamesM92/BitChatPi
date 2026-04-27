# ─── PROTOCOL CONTRACT ────────────────────────────────────────────────────────
# Derived from: app/src/main/java/com/bitchat/android/mesh/PacketRelayManager.kt
# Last verified against upstream commit: 66012e9 (2026-01-12)
# ──────────────────────────────────────────────────────────────────────────────
from __future__ import annotations
import random
import time
from collections import OrderedDict
from typing import Optional, Callable, Awaitable

from ..protocol.packet import BitchatPacket
from ..protocol.constants import (
    BROADCAST_ID,
    MAX_PROCESSED_MESSAGES,
    MESSAGE_TIMEOUT_MS,
    RELAY_HIGH_TTL_THRESHOLD,
    RELAY_SMALL_NET_MAX,
    RELAY_PROB_LE10, RELAY_PROB_LE30, RELAY_PROB_LE50,
    RELAY_PROB_LE100, RELAY_PROB_LARGE,
    PEER_ID_SIZE,
)


class RelayEngine:
    """
    Decides whether to relay an incoming packet and decrements TTL.

    Matches PacketRelayManager.kt logic exactly.
    Callers supply callbacks for broadcast and unicast forwarding.
    """

    def __init__(self, my_peer_id: bytes) -> None:
        self._my_peer_id = my_peer_id
        # LRU dedup cache: packet_key -> timestamp_ms
        self._seen: OrderedDict[bytes, int] = OrderedDict()
        # Callbacks set by the mesh layer
        self.broadcast_packet: Optional[Callable[[BitchatPacket, str], Awaitable[None]]] = None
        self.send_to_peer: Optional[Callable[[str, BitchatPacket], Awaitable[bool]]] = None
        self.get_network_size: Callable[[], int] = lambda: 1

    # ── Public ─────────────────────────────────────────────────────────────────

    async def handle_relay(
        self,
        packet: BitchatPacket,
        from_peer_id: str,
        relay_address: Optional[str] = None,
    ) -> None:
        """
        Process a received packet for possible relay.
        Only call this for packets NOT addressed to us.
        """
        # Drop our own packets
        if from_peer_id == self._my_peer_id.hex():
            return

        # Drop expired TTL
        if packet.ttl == 0:
            return

        # Decrement TTL
        relay_pkt = packet.with_ttl(packet.ttl - 1)

        # Deduplication
        key = _packet_key(relay_pkt)
        if self._is_seen(key):
            return
        self._mark_seen(key)

        # Source-based routing: if route is set and we are in it
        if relay_pkt.route:
            if _has_duplicate_hops(relay_pkt.route):
                return
            my_idx = _find_self_in_route(relay_pkt.route, self._my_peer_id)
            if my_idx >= 0:
                next_hop = _next_hop(relay_pkt, my_idx)
                if next_hop and self.send_to_peer:
                    sent = await self.send_to_peer(next_hop, relay_pkt)
                    if sent:
                        return
                    # fall through to broadcast if next hop unreachable

        if _should_relay(relay_pkt, self.get_network_size()):
            if self.broadcast_packet:
                await self.broadcast_packet(relay_pkt, from_peer_id)

    def is_addressed_to_me(self, packet: BitchatPacket) -> bool:
        """Return True if the packet's recipient_id matches our peer ID."""
        rid = packet.recipient_id
        if rid is None:
            return False
        if rid == BROADCAST_ID:
            return False
        return rid == self._my_peer_id

    # ── Dedup cache ────────────────────────────────────────────────────────────

    def _is_seen(self, key: bytes) -> bool:
        now = int(time.time() * 1000)
        if key in self._seen:
            ts = self._seen[key]
            if now - ts < MESSAGE_TIMEOUT_MS:
                return True
            del self._seen[key]
        return False

    def _mark_seen(self, key: bytes) -> None:
        now = int(time.time() * 1000)
        if key in self._seen:
            self._seen.move_to_end(key)
        self._seen[key] = now
        # Evict oldest entries beyond capacity
        while len(self._seen) > MAX_PROCESSED_MESSAGES:
            self._seen.popitem(last=False)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _packet_key(packet: BitchatPacket) -> bytes:
    """A dedup key that is stable across relay hops: sender + timestamp + type."""
    return packet.sender_id + packet.type.to_bytes(1, "big") + packet.timestamp.to_bytes(8, "big")


def _should_relay(packet: BitchatPacket, network_size: int) -> bool:
    """
    Probabilistic relay decision.
    Matches PacketRelayManager.shouldRelayPacket() exactly.
    """
    if packet.ttl >= RELAY_HIGH_TTL_THRESHOLD:
        return True
    if network_size <= RELAY_SMALL_NET_MAX:
        return True
    if network_size <= 10:
        prob = RELAY_PROB_LE10
    elif network_size <= 30:
        prob = RELAY_PROB_LE30
    elif network_size <= 50:
        prob = RELAY_PROB_LE50
    elif network_size <= 100:
        prob = RELAY_PROB_LE100
    else:
        prob = RELAY_PROB_LARGE
    return random.random() < prob


def _has_duplicate_hops(route: list[bytes]) -> bool:
    seen = set()
    for hop in route:
        h = bytes(hop)
        if h in seen:
            return True
        seen.add(h)
    return False


def _find_self_in_route(route: list[bytes], my_peer_id: bytes) -> int:
    for i, hop in enumerate(route):
        if bytes(hop) == my_peer_id[:PEER_ID_SIZE]:
            return i
    return -1


def _next_hop(packet: BitchatPacket, my_idx: int) -> Optional[str]:
    route = packet.route
    if route is None:
        return None
    next_idx = my_idx + 1
    if next_idx < len(route):
        return route[next_idx].hex()
    # We are the last intermediate hop; try final recipient
    if packet.recipient_id:
        return packet.recipient_id.hex()
    return None
