# Derived from: app/src/main/java/com/bitchat/android/mesh/PeerManager.kt
# (peer lifecycle constants from AppConstants.Mesh)
from __future__ import annotations
import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

from ..protocol.constants import STALE_PEER_TIMEOUT_MS, PEER_CLEANUP_INTERVAL_MS


@dataclass
class Peer:
    peer_id: str          # hex string, 16 chars (8 bytes)
    nickname: str
    first_seen: float     = field(default_factory=time.time)
    last_seen: float      = field(default_factory=time.time)
    rssi: Optional[int]   = None
    ble_address: Optional[str] = None   # MAC or BLE device address

    def touch(self) -> None:
        self.last_seen = time.time()

    @property
    def is_stale(self) -> bool:
        age_ms = (time.time() - self.last_seen) * 1000
        return age_ms > STALE_PEER_TIMEOUT_MS


class PeerManager:
    """
    Registry of currently active mesh peers.
    Peers are evicted after STALE_PEER_TIMEOUT_MS (180 s) of inactivity.
    """

    def __init__(self) -> None:
        self._peers: dict[str, Peer] = {}
        self._cleanup_task: Optional[asyncio.Task] = None

    def start(self) -> None:
        loop = asyncio.get_event_loop()
        self._cleanup_task = loop.create_task(self._cleanup_loop())

    def stop(self) -> None:
        if self._cleanup_task:
            self._cleanup_task.cancel()

    # ── Mutations ──────────────────────────────────────────────────────────────

    def add_or_update(self, peer_id: str, nickname: str,
                      ble_address: Optional[str] = None,
                      rssi: Optional[int] = None) -> Peer:
        if peer_id in self._peers:
            p = self._peers[peer_id]
            p.touch()
            if nickname:
                p.nickname = nickname
            if ble_address is not None:
                p.ble_address = ble_address
            if rssi is not None:
                p.rssi = rssi
        else:
            p = Peer(peer_id=peer_id, nickname=nickname,
                     ble_address=ble_address, rssi=rssi)
            self._peers[peer_id] = p
        return p

    def remove(self, peer_id: str) -> Optional[Peer]:
        return self._peers.pop(peer_id, None)

    def update_rssi(self, peer_id: str, rssi: int) -> None:
        if peer_id in self._peers:
            self._peers[peer_id].rssi = rssi
            self._peers[peer_id].touch()

    # ── Queries ────────────────────────────────────────────────────────────────

    def get(self, peer_id: str) -> Optional[Peer]:
        return self._peers.get(peer_id)

    def all_peers(self) -> list[Peer]:
        return list(self._peers.values())

    def count(self) -> int:
        return len(self._peers)

    def peer_ids(self) -> list[str]:
        return list(self._peers.keys())

    # ── Cleanup ────────────────────────────────────────────────────────────────

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(PEER_CLEANUP_INTERVAL_MS / 1000)
            stale = [pid for pid, p in self._peers.items() if p.is_stale]
            for pid in stale:
                self._peers.pop(pid, None)
