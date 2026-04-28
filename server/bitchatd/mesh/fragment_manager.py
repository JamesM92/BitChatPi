# ─── PROTOCOL CONTRACT ────────────────────────────────────────────────────────
# Derived from: app/src/main/java/com/bitchat/android/mesh/FragmentManager.kt
#           and app/src/main/java/com/bitchat/android/model/FragmentPayload.kt
# Last verified against upstream commit: 5b0a7d0 (2026-03-26)
# ──────────────────────────────────────────────────────────────────────────────
from __future__ import annotations
import asyncio
import base64
import hashlib
import json
import logging
import os
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Optional, Callable, Awaitable

log = logging.getLogger(__name__)

from ..protocol.packet import BitchatPacket
from ..protocol.codec import encode, decode, _unpad
from ..protocol.constants import (
    MessageType,
    FRAGMENT_SIZE_THRESHOLD,
    MAX_FRAGMENT_SIZE,
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
    sender_hex: str = ""
    frag_id: str = ""           # hex of the fragment set ID (unique per attempt)
    attempt: int = 0            # 1-based attempt number for this (sender, total, type)
    last_fragment_ms: float = 0 # updated on every received fragment
    fragments: dict[int, bytes] = field(default_factory=dict)
    cumulative_bytes: int = 0
    content_id: str = ""        # sha256(fragment_0_data)[:8] — stable across retry attempts


# ── FragmentManager ────────────────────────────────────────────────────────────

class FragmentManager:
    """
    Handles outbound fragmentation and inbound reassembly.
    100% iOS/Android compatible — matches FragmentManager.kt logic.
    """

    # Partial fragment sets are saved here when they expire close-to-complete.
    # If the sender retransmits the same ciphertext with a new frag_id (common
    # for automatic retry), the new set inherits these fragments and may complete
    # on the first new fragment that fills the gap.
    _RESCUE_TTL_MS       = 3_600_000  # 60 minutes — phones can retry 20-40 min later
    _RESCUE_MIN_FRACTION = 0.75       # rescue sets ≥ 75 % received
    _RESCUE_MAX_ENTRIES  = 20         # max number of partial sets on disk
    _RESCUE_MAX_BYTES    = 4 * 1024 * 1024  # 4 MB total raw fragment bytes

    def __init__(self, rescue_cache_path: Optional[str] = None) -> None:
        self._lock = Lock()
        self._sets: dict[str, _FragmentSet] = {}
        self._global_bytes: int = 0
        self._rescue_path: Optional[Path] = Path(rescue_cache_path) if rescue_cache_path else None
        # Called when a set expires incomplete:
        # (sender_hex, total, received, attempt_missing, combined_received, combined_missing, frag_id, attempt, original_type, content_id)
        self.on_fragment_expired: Optional[Callable[[str, int, int, list[int], int, list[int], str, int, int, str], None]] = None
        # Called when reassembly succeeds after ≥1 prior partial attempt:
        # (sender_hex, attempt, total, original_type, frag_id, content_id)
        self.on_fragment_completed: Optional[Callable[[str, int, int, int, str, str], None]] = None
        # Called when a new set is created with inherited rescue-cache fragments:
        # (sender_hex, total, original_type, attempt, inherited_count, frag_id, content_id)
        self.on_fragment_set_started: Optional[Callable[[str, int, int, int, int, str, str], None]] = None
        self._cleanup_task: Optional[asyncio.Task] = None
        # rescue cache: (sender_hex, total, orig_type) → (fragments, saved_at_ms)
        self._rescue: dict[tuple, dict[int, bytes]] = {}
        self._rescue_ts: dict[tuple, float] = {}
        # attempt counter: (sender_hex, total, orig_type) → attempt number
        self._attempt_count: dict[tuple, int] = {}
        if self._rescue_path:
            self._load_rescue_cache()

    # ── Rescue cache persistence ───────────────────────────────────────────────

    def _rescue_bytes_total(self) -> int:
        return sum(sum(len(v) for v in frags.values()) for frags in self._rescue.values())

    def _evict_rescue_if_needed(self) -> bool:
        """Remove oldest entries until within size limits. Returns True if anything was evicted."""
        evicted = False
        while self._rescue and (
            len(self._rescue) > self._RESCUE_MAX_ENTRIES
            or self._rescue_bytes_total() > self._RESCUE_MAX_BYTES
        ):
            oldest = min(self._rescue_ts, key=self._rescue_ts.__getitem__)
            log.info("FRAGMENT rescue cache evict  sender=%s  (limit enforced)", oldest[0][:8])
            self._rescue.pop(oldest, None)
            self._rescue_ts.pop(oldest, None)
            self._attempt_count.pop(oldest, None)
            evicted = True
        return evicted

    def _save_rescue_cache(self) -> None:
        if not self._rescue_path:
            return
        with self._lock:
            entries = []
            for key, frags in self._rescue.items():
                lowest     = min(frags.keys()) if frags else None
                content_id = (hashlib.sha256(frags[lowest]).hexdigest()[:8]
                              if lowest is not None else "")
                entries.append({
                    "sender":     key[0],
                    "total":      key[1],
                    "orig_type":  key[2],
                    "ts":         self._rescue_ts.get(key, 0.0),
                    "attempt":    self._attempt_count.get(key, 0),
                    "content_id": content_id,
                    "fragments":  {str(i): base64.b64encode(d).decode() for i, d in frags.items()},
                })
        entries.sort(key=lambda e: e["ts"], reverse=True)
        try:
            self._rescue_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._rescue_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"version": 1, "entries": entries}, separators=(",", ":")))
            tmp.replace(self._rescue_path)
            log.debug("FRAGMENT rescue cache saved  entries=%d", len(entries))
        except Exception:
            log.exception("Failed to save rescue cache to %s", self._rescue_path)

    def _load_rescue_cache(self) -> None:
        if not self._rescue_path or not self._rescue_path.exists():
            return
        try:
            data = json.loads(self._rescue_path.read_text())
            now_ms = time.time() * 1000
            loaded = 0
            for entry in data.get("entries", []):
                key = (entry["sender"], int(entry["total"]), int(entry["orig_type"]))
                ts  = float(entry.get("ts", 0))
                if now_ms - ts > self._RESCUE_TTL_MS:
                    continue
                frags = {int(i): base64.b64decode(v) for i, v in entry.get("fragments", {}).items()}
                if frags:
                    self._rescue[key]        = frags
                    self._rescue_ts[key]     = ts
                    self._attempt_count[key] = int(entry.get("attempt", 0))
                    loaded += 1
            self._evict_rescue_if_needed()
            log.info("FRAGMENT rescue cache loaded  entries=%d  path=%s", loaded, self._rescue_path)
        except Exception:
            log.exception("Failed to load rescue cache from %s", self._rescue_path)

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
        set_started_meta = None

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
                now_ms = time.time() * 1000
                sender_hex = packet.sender_id.hex()
                rescue_key = (sender_hex, total, original_type)
                inherited: dict[int, bytes] = {}
                if rescue_key in self._rescue:
                    if now_ms - self._rescue_ts.get(rescue_key, 0) < self._RESCUE_TTL_MS:
                        inherited = dict(self._rescue[rescue_key])
                attempt = self._attempt_count.get(rescue_key, 0) + 1
                self._attempt_count[rescue_key] = attempt
                self._sets[frag_key] = _FragmentSet(
                    original_type=original_type,
                    total=total,
                    timestamp_ms=now_ms,
                    last_fragment_ms=now_ms,
                    sender_hex=sender_hex,
                    frag_id=frag_key,
                    attempt=attempt,
                )
                s = self._sets[frag_key]
                if inherited:
                    s.fragments.update(inherited)
                    s.cumulative_bytes = sum(len(v) for v in inherited.values())
                    self._global_bytes += s.cumulative_bytes
                    lowest = min(inherited.keys())
                    s.content_id = hashlib.sha256(inherited[lowest]).hexdigest()[:8]
                    set_started_meta = (sender_hex, total, original_type,
                                        attempt, len(inherited), frag_key, s.content_id)
                    log.info("FRAGMENT new set from %s  id=%s  total=%d  orig_type=0x%02x"
                             "  attempt=%d  (inherited %d/%d from rescue cache)",
                             sender_hex, frag_key[:8], total, original_type,
                             attempt, len(inherited), total)
                else:
                    log.info("FRAGMENT new set from %s  id=%s  total=%d  orig_type=0x%02x"
                             "  attempt=%d",
                             sender_hex, frag_key[:8], total, original_type, attempt)

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
            s.last_fragment_ms = time.time() * 1000
            self._global_bytes = max(0, self._global_bytes + delta)
            if index == 0 and not s.content_id:
                s.content_id = hashlib.sha256(data).hexdigest()[:8]

            # Check if complete
            result_pkt    = None
            completed_meta = None
            rescue_cleared = False
            if len(s.fragments) == total:
                reassembled    = b"".join(s.fragments[i] for i in range(total))
                completed_meta = (s.sender_hex, s.attempt, s.total, s.original_type, s.frag_id, s.content_id)
                self._remove_set(frag_key)

                # Purge any other in-progress sets for the same image (prior partial
                # attempts that hadn't expired yet).  Without this, a stale attempt-1
                # set fires on_fragment_expired ~30 s after the image already completed.
                sender_hex_c, _, total_c, orig_type_c = completed_meta[:4]
                stale = [k for k, fs in self._sets.items()
                         if fs.sender_hex == sender_hex_c
                         and fs.total == total_c
                         and fs.original_type == orig_type_c]
                for k in stale:
                    log.info("FRAGMENT purging stale set %s — superseded by completed reassembly", k[:8])
                    self._remove_set(k)

                rescue_key = (sender_hex_c, total_c, orig_type_c)
                if rescue_key in self._rescue:
                    self._rescue.pop(rescue_key, None)
                    self._rescue_ts.pop(rescue_key, None)
                    # Keep _attempt_count so any late-arriving fragments from the
                    # same transmission create a correctly-numbered attempt rather
                    # than resetting to 1 and firing spurious notifications.
                    rescue_cleared = True

                original = decode(reassembled)
                if original is not None:
                    result_pkt = original.with_ttl(0)

        # ── Outside the lock ──────────────────────────────────────────────────
        if set_started_meta is not None and self.on_fragment_set_started:
            try:
                self.on_fragment_set_started(*set_started_meta)
            except Exception:
                log.exception("on_fragment_set_started raised")

        if rescue_cleared and self._rescue_path:
            self._save_rescue_cache()

        if result_pkt is not None and completed_meta is not None:
            sender_hex, attempt, _total, orig_type, frag_id, content_id = completed_meta
            if attempt > 1 and self.on_fragment_completed:
                try:
                    self.on_fragment_completed(sender_hex, attempt, _total, orig_type, frag_id, content_id)
                except Exception:
                    log.exception("on_fragment_completed raised")

        return result_pkt

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
        now_ms = time.time() * 1000
        with self._lock:
            # Evict stale rescue-cache entries first
            stale_rescue = [k for k, ts in self._rescue_ts.items()
                            if now_ms - ts > self._RESCUE_TTL_MS]
            for k in stale_rescue:
                self._rescue.pop(k, None)
                self._rescue_ts.pop(k, None)
                self._attempt_count.pop(k, None)

            # Inactivity timeout: how long since the last fragment arrived, vs how long
            # the full transfer should take at a conservative 30 frags/sec with 1.5x buffer.
            # Floor of 30 s covers BLE reconnect gaps.  This replaces the old fixed
            # FRAGMENT_TIMEOUT_MS (5 min) which was creation-time based.
            def _inactivity_limit_ms(s: _FragmentSet) -> float:
                return max(s.total / 30.0 * 1.5, 30) * 1000

            expired = [k for k, s in self._sets.items()
                       if now_ms - s.last_fragment_ms > _inactivity_limit_ms(s)]
            expired_callbacks: list[tuple[str, int, int, list[int]]] = []
            for k in expired:
                s = self._sets[k]
                received = len(s.fragments)
                missing = sorted(set(range(s.total)) - set(s.fragments.keys()))
                log.warning("FRAGMENT set expired  id=%s  received=%d/%d  orig_type=0x%02x"
                            "  missing=%s",
                            k[:8], received, s.total, s.original_type, missing)

                # Merge into rescue cache and compute the combined missing set.
                # Always merge regardless of threshold so the combined view is accurate.
                if not s.content_id and s.fragments:
                    lowest = min(s.fragments.keys())
                    s.content_id = hashlib.sha256(s.fragments[lowest]).hexdigest()[:8]

                rescue_key = (s.sender_hex, s.total, s.original_type) if s.sender_hex else None
                if rescue_key is not None:
                    existing = self._rescue.get(rescue_key, {})
                    merged = {**existing, **s.fragments}
                    combined_received = len(merged)
                    combined_missing  = sorted(set(range(s.total)) - set(merged.keys()))
                    # Only persist to rescue cache if ≥ 75% received — below that threshold
                    # the data is too sparse to be useful for future inheritance.
                    if received >= s.total * self._RESCUE_MIN_FRACTION:
                        self._rescue[rescue_key] = merged
                        self._rescue_ts[rescue_key] = now_ms
                        log.info("FRAGMENT rescue cache saved  sender=%s  total=%d  frags=%d",
                                 s.sender_hex[:8], s.total, combined_received)
                    # Report both per-attempt and combined state.
                    if combined_missing and self.on_fragment_expired:
                        expired_callbacks.append(
                            (s.sender_hex, s.total, received, missing,
                             combined_received, combined_missing,
                             s.frag_id, s.attempt, s.original_type, s.content_id)
                        )
                self._remove_set(k)
        # Outside the lock: enforce size limits, persist, fire callbacks
        rescue_changed = bool(stale_rescue) or bool(expired)
        if rescue_changed and self._rescue_path:
            with self._lock:
                self._evict_rescue_if_needed()
            self._save_rescue_cache()
        if expired_callbacks and self.on_fragment_expired:
            for args in expired_callbacks:
                try:
                    self.on_fragment_expired(*args)
                except Exception:
                    log.exception("on_fragment_expired raised")
