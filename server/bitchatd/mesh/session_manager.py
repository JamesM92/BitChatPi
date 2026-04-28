"""
Per-peer session management for the BitChat mesh daemon.

Owns:
  - Noise XX session state (handshake, encrypt/decrypt)
  - Known-peers registry
  - Queued outbound sends (waiting on session establishment)
  - All packet-handling logic for packets addressed to this node
  - IPC command handling for send / broadcast / send_file

daemon.py owns BLE I/O and process lifecycle; it supplies send_packet,
fragment_and_send, and publish as callbacks at construction time.
"""
from __future__ import annotations
import logging
import struct
import time
import uuid
import zlib
from pathlib import Path
from typing import Callable, Optional

from ..protocol.constants import MessageType, MESSAGE_TTL_HOPS
from ..protocol.packet import BitchatPacket
from ..protocol.codec import encode
from ..protocol.announce import encode_announce, decode_announce
from ..crypto.noise_session import NoiseSession
from ..crypto.signing import sign

log = logging.getLogger(__name__)

_ANNOUNCE_COOLDOWN = 10.0   # seconds — minimum gap between ANNOUNCE echoes per peer
_PENDING_SEND_TTL  = 600.0  # seconds — drop queued messages after this long

_FILE_MAGIC: list[tuple[bytes, str, str]] = [
    (b'\xff\xd8\xff',           ".jpg",  "image/jpeg"),
    (b'\x89PNG\r\n\x1a\n',     ".png",  "image/png"),
    (b'GIF87a',                 ".gif",  "image/gif"),
    (b'GIF89a',                 ".gif",  "image/gif"),
    (b'RIFF',                   "",      ""),           # sub-type detected in _sniff_file
    (b'\x00\x00\x00\x0cftyp',  ".m4a",  "audio/mp4"),  # M4A (short box)
    (b'\x00\x00\x00\x18ftyp',  ".m4a",  "audio/mp4"),
    (b'\x00\x00\x00\x1cftyp',  ".m4a",  "audio/mp4"),
    (b'\x00\x00\x00\x20ftyp',  ".m4a",  "audio/mp4"),
    (b'\x00\x00\x00\x24ftyp',  ".mp4",  "video/mp4"),
    (b'\x00\x00\x00\x28ftyp',  ".mp4",  "video/mp4"),
    (b'ID3',                    ".mp3",  "audio/mpeg"),
    (b'\xff\xfb',               ".mp3",  "audio/mpeg"),
    (b'\xff\xf2',               ".mp3",  "audio/mpeg"),
    (b'\xff\xf3',               ".mp3",  "audio/mpeg"),
    (b'\xff\xf1',               ".aac",  "audio/aac"),  # ADTS AAC MPEG-4
    (b'\xff\xf9',               ".aac",  "audio/aac"),  # ADTS AAC MPEG-2
    (b'OggS',                   ".ogg",  "audio/ogg"),
    (b'fLaC',                   ".flac", "audio/flac"),
    (b'\x1a\x45\xdf\xa3',      ".webm", "video/webm"),
]


# ── Wire-format helpers ────────────────────────────────────────────────────────

def _encode_message_payload(content: str, msg_id: str | None = None) -> tuple[bytes, str]:
    """Return (wire_bytes, msg_id). Caller uses msg_id to correlate receipts."""
    if msg_id is None:
        msg_id = str(uuid.uuid4())
    id_b      = msg_id.encode()
    content_b = content.encode()
    out = bytearray()
    out += struct.pack(">B", 0x01)
    out += struct.pack(">H", len(id_b))
    out += id_b
    out += struct.pack(">B", 1)
    out += struct.pack(">B", len(content_b))
    out += content_b
    return bytes(out), msg_id


def _encode_file_payload(data: bytes) -> tuple[bytes, str]:
    """Return (wire_bytes, msg_id) for a binary file payload."""
    msg_id = str(uuid.uuid4())
    id_b = msg_id.encode()
    out = bytearray()
    out += struct.pack(">B", 0x00)
    out += struct.pack(">H", len(id_b))
    out += id_b
    out += struct.pack(">B", 2)
    out += data
    return bytes(out), msg_id


def _sniff_file(data: bytes) -> tuple[str, str]:
    """Return (ext, mime) by inspecting magic bytes, defaulting to .bin."""
    for magic, ext, mime in _FILE_MAGIC:
        if data[:len(magic)] == magic:
            if magic == b'RIFF':
                if len(data) >= 12:
                    sub = data[8:12]
                    if sub == b'WEBP':
                        return ".webp", "image/webp"
                    if sub == b'WAVE':
                        return ".wav", "audio/wav"
                continue
            return ext, mime
    # ISO Base Media ftyp box — box size is variable (first 4 bytes), brand at offset 8.
    # Handles M4A, AAC in MP4, and generic MP4 regardless of box size.
    if len(data) >= 12 and data[4:8] == b'ftyp':
        brand = data[8:12]
        if brand in (b'M4A ', b'M4B ', b'M4P ', b'aac ', b'AACP'):
            return ".m4a", "audio/mp4"
        if brand in (b'M4V ', b'f4v '):
            return ".m4v", "video/mp4"
        return ".mp4", "video/mp4"
    return ".bin", "application/octet-stream"


_FILES_DIR = Path.home() / ".config" / "bitchatd" / "files"


def _save_received_file(msg_id: str, data: bytes) -> tuple[str, str]:
    """Save binary data to ~/.config/bitchatd/files/, return (path, mime)."""
    _FILES_DIR.mkdir(parents=True, exist_ok=True)
    ext, mime = _sniff_file(data)
    path = str(_FILES_DIR / f"bitchat_{msg_id[:8]}{ext}")
    with open(path, "wb") as fh:
        fh.write(data)
    return path, mime


def _parse_incoming(plaintext: bytes) -> tuple[str, object] | None:
    """
    Parse a decrypted NOISE_ENCRYPTED inner payload.
    Returns ('receipt', ref_uuid_str)
          | ('message', (msg_id, content_str))
          | ('file',    (msg_id, path, mime, name))
          | None
    """
    # ── Receipt: exactly 37 bytes, first byte 0x02 (read) or 0x03 (delivery) ─
    if len(plaintext) == 37 and plaintext[0] in (0x02, 0x03):
        try:
            return ('receipt', plaintext[1:37].decode('ascii'))
        except Exception as exc:
            log.debug("Receipt parse failed: %s", exc)

    # ── Standard framing: flags(1) | id_len(uint16 BE) | id(36) | msg_type(1) ─
    if len(plaintext) >= 41:
        try:
            id_len = struct.unpack_from(">H", plaintext, 1)[0]
            if id_len == 36:
                msg_id   = plaintext[3:39].decode('ascii')
                msg_type = plaintext[39]
                rest     = plaintext[40:]
                if msg_type == 1:
                    if rest:
                        cont_len = rest[0]
                        content  = rest[1:1 + cont_len].decode('utf-8')
                        return ('message', (msg_id, content))
                else:
                    log.info("NOISE binary msg_type=0x%02x  %d bytes  hex=%s…",
                             msg_type, len(rest), rest[:16].hex())
                    data = rest
                    for off in range(min(32, len(rest))):
                        if _sniff_file(rest[off:])[1] != "application/octet-stream":
                            data = rest[off:]
                            break
                    path, mime = _save_received_file(msg_id, data)
                    name = f"file{_sniff_file(data)[0]}"
                    log.info("Binary file  mime=%s  %d bytes  saved=%s", mime, len(data), path)
                    return ('file', (msg_id, path, mime, name))
        except Exception as exc:
            log.debug("Standard framing parse failed: %s", exc)

    # ── FILE_TRANSFER TLV format (magic 0x2001) ───────────────────────────────
    if len(plaintext) >= 7 and plaintext[0] == 0x20 and plaintext[1] == 0x01:
        try:
            off = 2
            fname = mime_type = b""
            file_data = b""
            while off + 3 <= len(plaintext):
                tag = plaintext[off]; off += 1
                if tag == 0x04:
                    if off + 4 > len(plaintext):
                        break
                    dlen = struct.unpack_from(">I", plaintext, off)[0]; off += 4
                    file_data = plaintext[off:off + dlen]
                    break
                flen = struct.unpack_from(">H", plaintext, off)[0]; off += 2
                val = plaintext[off:off + flen]; off += flen
                if tag == 0x00:
                    fname = val
                elif tag == 0x03:
                    mime_type = val
            if file_data:
                msg_id = str(uuid.uuid4())
                detected_ext, detected_mime = _sniff_file(file_data)
                actual_mime = mime_type.decode(errors="replace") if mime_type else detected_mime
                path, _ = _save_received_file(msg_id, file_data)
                name = fname.decode(errors="replace") if fname else f"file{detected_ext}"
                log.info("FILE_TRANSFER TLV: name=%s  mime=%s  %d bytes",
                         name, actual_mime, len(file_data))
                return ('file', (msg_id, path, actual_mime, name))
        except Exception as exc:
            log.debug("FILE_TRANSFER TLV parse failed: %s", exc)

    # ── Legacy BitchatMessage: flags(1)|ts(8)|id_len(uint8)|id|sender|content ─
    if len(plaintext) >= 14:
        try:
            id_len = plaintext[9]
            end_id = 10 + id_len
            if id_len > 0 and end_id + 3 <= len(plaintext):
                msg_id = plaintext[10:end_id].decode('ascii')
                sender_len = plaintext[end_id]
                content_start = end_id + 1 + sender_len
                if content_start + 2 <= len(plaintext):
                    content_len = struct.unpack_from(">H", plaintext, content_start)[0]
                    content_data = plaintext[content_start + 2:content_start + 2 + content_len]
                    if content_data:
                        try:
                            return ('message', (msg_id, content_data.decode('utf-8')))
                        except UnicodeDecodeError:
                            data = content_data
                            for off in (0, 1, 4, 8):
                                if off < len(data) and _sniff_file(data[off:])[1] != "application/octet-stream":
                                    data = data[off:]
                                    break
                            path, mime = _save_received_file(msg_id, data)
                            name = f"file{_sniff_file(data)[0]}"
                            return ('file', (msg_id, path, mime, name))
        except Exception as exc:
            log.debug("Legacy framing parse failed: %s", exc)

    # ── Last resort: scan first 512 bytes of payload for a known file signature ─
    if len(plaintext) > 8:
        for off in range(min(512, len(plaintext) - 4)):
            ext, mime = _sniff_file(plaintext[off:])
            if mime != "application/octet-stream":
                msg_id = str(uuid.uuid4())
                path, _ = _save_received_file(msg_id, plaintext[off:])
                name = f"file{ext}"
                log.info("Raw file detected at offset %d  mime=%s", off, mime)
                return ('file', (msg_id, path, mime, name))

    # ── Decompression fallback: Android may compress plaintext before encrypting ─
    if len(plaintext) >= 10:
        for wbits in (15, -15, 47):
            try:
                dec = zlib.decompress(plaintext, wbits=wbits)
            except zlib.error:
                continue
            if len(dec) > len(plaintext):
                log.info("Decompressed plaintext  %d→%d bytes  hex=%s…",
                         len(plaintext), len(dec), dec[:16].hex())
                for off in range(min(512, len(dec) - 4)):
                    ext, mime = _sniff_file(dec[off:])
                    if mime != "application/octet-stream":
                        msg_id = str(uuid.uuid4())
                        path, _ = _save_received_file(msg_id, dec[off:])
                        name = f"file{ext}"
                        log.info("Decompressed file at offset %d  mime=%s", off, mime)
                        return ('file', (msg_id, path, mime, name))
            break

    # ── Binary blob fallback: large content that isn't valid UTF-8 ──────────────
    if len(plaintext) >= 500:
        try:
            plaintext.decode('utf-8')
        except UnicodeDecodeError:
            msg_id = str(uuid.uuid4())
            path, mime = _save_received_file(msg_id, plaintext)
            ext, _ = _sniff_file(plaintext)
            name = f"file{ext}"
            log.info("Binary blob saved (unknown format): mime=%s  %d bytes  saved=%s",
                     mime, len(plaintext), path)
            return ('file', (msg_id, path, mime, name))

    log.info("_parse_incoming: no format matched  len=%d  hex=%s…",
             len(plaintext), plaintext[:32].hex())
    return None


# ── SessionManager ─────────────────────────────────────────────────────────────

class SessionManager:
    """
    Holds all per-peer Noise session state and handles incoming/outgoing
    BitChat protocol messages.

    daemon.py instantiates one of these and calls:
      handle_packet(pkt)   — for every received BLE packet addressed to us
      handle_command(cmd)  — for every IPC command
    """

    def __init__(
        self,
        identity,                                          # crypto.identity.Identity
        nickname: str,
        send_packet: Callable[[BitchatPacket], None],      # sends a single packet
        fragment_and_send: Callable[[BitchatPacket], None],# fragments if needed, then sends
        publish: Callable[[dict], None],                   # pushes an IPC event
    ) -> None:
        self._identity          = identity
        self._nickname          = nickname
        self._send_packet       = send_packet
        self._fragment_and_send = fragment_and_send
        self._publish           = publish

        self._sessions:           dict[str, NoiseSession]              = {}
        self._peers:              dict[str, str]                       = {}
        self._peer_noise_pub:     dict[str, bytes]                     = {}
        self._peer_last_seen:     dict[str, float]                     = {}
        self._last_announced:     dict[str, float]                     = {}
        self._last_msg1:          dict[str, bytes]                     = {}
        self._pending_msg2:       dict[str, bytes]                     = {}
        self._pending_sends:      dict[str, list[tuple[str, str, float]]] = {}
        self._session_timestamps: dict[str, float]                     = {}
        self._decrypt_fail_count: dict[str, int]                       = {}

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def peers(self) -> dict[str, str]:
        return self._peers

    def clear_announce_cooldown(self) -> None:
        """Bypass per-peer cooldown so the next ANNOUNCE goes out immediately."""
        self._last_announced.clear()

    def has_session(self, peer_hex: str) -> bool:
        """Return True if we have any state for this peer (session or peer registry)."""
        return peer_hex in self._sessions or peer_hex in self._peers

    # ── Outgoing packet builders ───────────────────────────────────────────────

    def make_announce(self) -> BitchatPacket:
        payload = encode_announce(
            self._nickname,
            self._identity.noise_keypair.public.data,
            self._identity.sign_public,
        )
        unsigned = BitchatPacket(
            type=MessageType.ANNOUNCE,
            sender_id=self._identity.peer_id,
            payload=payload,
            ttl=0,
            timestamp=int(time.time() * 1000),
        )
        sig_data = encode(unsigned, pad=True)
        sig = sign(self._identity.sign_private, sig_data) if sig_data else None
        return BitchatPacket(
            type=MessageType.ANNOUNCE,
            sender_id=self._identity.peer_id,
            payload=payload,
            ttl=MESSAGE_TTL_HOPS,
            timestamp=unsigned.timestamp,
            signature=sig,
        )

    def make_leave(self) -> BitchatPacket:
        return BitchatPacket(
            type=MessageType.LEAVE,
            sender_id=self._identity.peer_id,
            payload=b'',
            ttl=MESSAGE_TTL_HOPS,
            timestamp=int(time.time() * 1000),
        )

    def make_message_packet(self, content: str) -> BitchatPacket:
        ts = int(time.time() * 1000)
        payload = content.encode("utf-8")
        unsigned = BitchatPacket(
            type=MessageType.MESSAGE,
            sender_id=self._identity.peer_id,
            payload=payload,
            ttl=0,
            timestamp=ts,
        )
        sig_data = encode(unsigned, pad=True)
        sig = sign(self._identity.sign_private, sig_data) if sig_data else None
        return BitchatPacket(
            type=MessageType.MESSAGE,
            sender_id=self._identity.peer_id,
            payload=payload,
            ttl=MESSAGE_TTL_HOPS,
            timestamp=ts,
            signature=sig,
        )

    # ── Incoming packet dispatch ───────────────────────────────────────────────

    def handle_packet(self, pkt: BitchatPacket) -> None:
        peer_hex = pkt.sender_id.hex()
        self._peer_last_seen[peer_hex] = time.time()
        if pkt.type == MessageType.ANNOUNCE:
            self._handle_announce(pkt, peer_hex)
        elif pkt.type == MessageType.NOISE_HANDSHAKE:
            self._handle_noise_handshake(pkt, peer_hex)
        elif pkt.type == MessageType.NOISE_ENCRYPTED:
            self._handle_noise_encrypted(pkt, peer_hex)
        elif pkt.type == MessageType.FILE_TRANSFER:
            self._handle_file_transfer(pkt, peer_hex)
        elif pkt.type == MessageType.MESSAGE:
            self._handle_public_message(pkt, peer_hex)
        elif pkt.type == MessageType.LEAVE:
            self._handle_leave(pkt, peer_hex)
        elif pkt.type == MessageType.REQUEST_SYNC:
            self._handle_request_sync(pkt, peer_hex)
        else:
            log.info("PKT type=0x%02x  ttl=%d  from=%s  %d bytes payload",
                     pkt.type, pkt.ttl, peer_hex, len(pkt.payload))

    def _handle_announce(self, pkt: BitchatPacket, peer_hex: str) -> None:
        ann = decode_announce(pkt.payload)
        if ann:
            log.info("ANNOUNCE from %s  nick=%s  noise_pub=%s…",
                     peer_hex, ann.nickname, ann.noise_pub.hex()[:12])
            if self._peers.get(peer_hex) != ann.nickname:
                self._peers[peer_hex] = ann.nickname
                self._publish({"event": "peer", "action": "seen",
                               "peer_id": peer_hex, "nick": ann.nickname,
                               "last_seen": self._peer_last_seen.get(peer_hex, time.time())})
            # Detect noise_pub change — peer restarted with a new noise keypair.
            # Reset the session so a fresh handshake can happen immediately.
            old_noise_pub = self._peer_noise_pub.get(peer_hex)
            if old_noise_pub is not None and old_noise_pub != ann.noise_pub:
                log.info("Noise public key changed for %s — resetting stale session", peer_hex)
                self._sessions.pop(peer_hex, None)
                self._pending_msg2.pop(peer_hex, None)
                self._last_msg1.pop(peer_hex, None)
                self._session_timestamps.pop(peer_hex, None)
            self._peer_noise_pub[peer_hex] = ann.noise_pub
        else:
            log.warning("Malformed ANNOUNCE from %s", peer_hex)

        now = time.time()
        if now - self._last_announced.get(peer_hex, 0) >= _ANNOUNCE_COOLDOWN:
            self._send_packet(self.make_announce())
            self._last_announced[peer_hex] = now
        else:
            log.debug("ANNOUNCE cooldown for %s — skipping echo", peer_hex)

        existing = self._sessions.get(peer_hex)
        if existing is not None and existing.is_established:
            log.debug("ANNOUNCE from %s — existing established session kept", peer_hex)

    def _handle_noise_handshake(self, pkt: BitchatPacket, peer_hex: str) -> None:
        sess = self._sessions.get(peer_hex)

        # Create responder session on first msg1 even if ANNOUNCE wasn't seen first.
        if sess is None and len(pkt.payload) == 32:
            sess = NoiseSession.create_responder(self._identity.noise_keypair)
            self._sessions[peer_hex] = sess
            self._session_timestamps[peer_hex] = time.time()
            log.info("NOISE_HANDSHAKE msg1 from new peer %s — created responder session", peer_hex)
        elif sess is None:
            log.warning("NOISE_HANDSHAKE from unknown peer %s (non-msg1, %d bytes) — ignoring",
                        peer_hex, len(pkt.payload))
            return

        log.info("NOISE_HANDSHAKE from %s  %d bytes", peer_hex, len(pkt.payload))

        # If session is already established and we receive a new msg1 (32-byte
        # ephemeral), the peer wants a fresh handshake — e.g. after hitting its
        # rekey limit or restarting.  Reset our side so we can respond correctly.
        # Without this, read_handshake_message() fails on the established session,
        # deletes it, and the next NOISE_ENCRYPTED causes a needless LEAVE cycle.
        if sess.is_established and len(pkt.payload) == 32:
            log.info("New msg1 from %s while session established — resetting for re-handshake",
                     peer_hex)
            sess = NoiseSession.create_responder(self._identity.noise_keypair)
            self._sessions[peer_hex] = sess
            self._pending_msg2.pop(peer_hex, None)
            self._last_msg1.pop(peer_hex, None)

        # Detect duplicate msg1 — phone retried before receiving our msg2.
        if not sess.is_established and len(pkt.payload) == 32:
            cached_msg2 = self._pending_msg2.get(peer_hex)
            last_m1     = self._last_msg1.get(peer_hex)
            if last_m1 == pkt.payload and cached_msg2 is not None:
                log.info("Phone retried msg1 for %s — re-sending cached msg2", peer_hex)
                self._send_packet(BitchatPacket(
                    type=MessageType.NOISE_HANDSHAKE,
                    sender_id=self._identity.peer_id,
                    recipient_id=pkt.sender_id,
                    payload=cached_msg2,
                    ttl=MESSAGE_TTL_HOPS,
                    timestamp=int(time.time() * 1000),
                ))
                return
            elif last_m1 is not None or cached_msg2 is not None:
                log.info("Phone sent new msg1 for %s — resetting session", peer_hex)
                sess = NoiseSession.create_responder(self._identity.noise_keypair)
                self._sessions[peer_hex] = sess
                self._pending_msg2.pop(peer_hex, None)
                self._last_msg1.pop(peer_hex, None)

        if sess.read_handshake_message(pkt.payload) is None:
            log.error("Handshake read failed for %s", peer_hex)
            del self._sessions[peer_hex]
            self._pending_msg2.pop(peer_hex, None)
            self._last_msg1.pop(peer_hex, None)
            return

        if len(pkt.payload) == 32:
            self._last_msg1[peer_hex] = pkt.payload

        if sess.is_established:
            log.info("Noise session ESTABLISHED with %s  remote_pub=%s…",
                     peer_hex, (sess.remote_static_public or b'').hex()[:12])
            self._pending_msg2.pop(peer_hex, None)
            self._last_msg1.pop(peer_hex, None)
            self._drain_pending_sends(peer_hex, sess)
            return

        # msg1 received — write msg2.
        self._session_timestamps[peer_hex] = time.time()
        outgoing = sess.write_handshake_message()
        if outgoing is None:
            log.error("Handshake write failed for %s", peer_hex)
            del self._sessions[peer_hex]
            self._pending_msg2.pop(peer_hex, None)
            self._last_msg1.pop(peer_hex, None)
            return

        self._pending_msg2[peer_hex] = outgoing
        self._send_packet(BitchatPacket(
            type=MessageType.NOISE_HANDSHAKE,
            sender_id=self._identity.peer_id,
            recipient_id=pkt.sender_id,
            payload=outgoing,
            ttl=MESSAGE_TTL_HOPS,
            timestamp=int(time.time() * 1000),
        ))
        log.info("Noise msg2 sent to %s  %d bytes", peer_hex, len(outgoing))

    def cancel_pending_sends(self, to_hex: str) -> int:
        """Remove all queued outbound messages for to_hex. Returns count cancelled."""
        cancelled = self._pending_sends.pop(to_hex, [])
        return len(cancelled)

    def _drain_pending_sends(self, peer_hex: str, sess: NoiseSession) -> None:
        queued = self._pending_sends.pop(peer_hex, [])
        now    = time.time()
        queued = [(c, m, t) for c, m, t in queued if now - t < _PENDING_SEND_TTL]
        if not queued:
            return
        try:
            recipient_id = bytes.fromhex(peer_hex)
        except ValueError:
            return
        for content, msg_id, _ in queued:
            payload, _ = _encode_message_payload(content, msg_id)
            wire = sess.encrypt(payload)
            if wire:
                self._send_packet(BitchatPacket(
                    type=MessageType.NOISE_ENCRYPTED,
                    sender_id=self._identity.peer_id,
                    recipient_id=recipient_id,
                    payload=wire,
                    ttl=MESSAGE_TTL_HOPS,
                    timestamp=int(time.time() * 1000),
                ))
                log.info("Delivered queued message to %s: %r", peer_hex[:12], content)

    def _handle_noise_encrypted(self, pkt: BitchatPacket, peer_hex: str) -> None:
        sess = self._sessions.get(peer_hex)
        if sess is None or not sess.is_established:
            # Phone has a stale session; we have none. Send LEAVE so the phone
            # drops its cached session state, then ANNOUNCE so it knows our
            # current noise public key and can initiate a fresh handshake.
            self._send_packet(self.make_leave())
            self._sessions[peer_hex] = NoiseSession.create_responder(
                self._identity.noise_keypair)
            self._last_announced.pop(peer_hex, None)
            self._send_packet(self.make_announce())
            self._last_announced[peer_hex] = time.time()
            log.warning("NOISE_ENCRYPTED from %s with no/stale session "
                        "— sent LEAVE+ANNOUNCE to force re-handshake", peer_hex)
            return

        plaintext = sess.decrypt(pkt.payload)
        if plaintext is None:
            n = self._decrypt_fail_count.get(peer_hex, 0) + 1
            self._decrypt_fail_count[peer_hex] = n
            log.warning("Decrypt failed from %s (consecutive=%d)", peer_hex, n)
            if n >= 3:
                # Cipher state is permanently desynchronised — reset and force a
                # fresh handshake so subsequent messages (including auto-replies)
                # can be encrypted/decrypted correctly.
                self._decrypt_fail_count.pop(peer_hex, None)
                self._send_packet(self.make_leave())
                self._sessions[peer_hex] = NoiseSession.create_responder(
                    self._identity.noise_keypair)
                self._last_announced.pop(peer_hex, None)
                self._send_packet(self.make_announce())
                self._last_announced[peer_hex] = time.time()
                log.warning("Decrypt failed 3x from %s — reset session, sent LEAVE+ANNOUNCE",
                            peer_hex)
            return

        self._decrypt_fail_count.pop(peer_hex, None)  # reset on successful decrypt

        parsed = _parse_incoming(plaintext)
        if parsed is None:
            log.warning("Unknown NOISE_ENCRYPTED format from %s  len=%d  hex=%s…",
                        peer_hex, len(plaintext), plaintext[:64].hex())
            return

        kind, value = parsed

        if kind == 'receipt':
            receipt_type = "read" if plaintext[0] == 0x02 else "delivery"
            log.info("RECEIPT %s from %s  ref=%s…", receipt_type, peer_hex, value[:8])
            self._publish({"event": "receipt", "type": receipt_type,
                           "ref": value, "from": peer_hex})
            return

        if kind == 'file':
            msg_id, path, mime, name = value
            log.info("FILE from %s  mime=%s  saved=%s", peer_hex, mime, path)
            self._publish({"event": "file", "from": peer_hex,
                           "nick": self._peers.get(peer_hex, ""),
                           "path": path, "mime": mime, "name": name})
            try:
                for receipt_byte in (0x03, 0x02):
                    receipt_wire = sess.encrypt(bytes([receipt_byte]) + msg_id.encode())
                    if receipt_wire:
                        self._send_packet(BitchatPacket(
                            type=MessageType.NOISE_ENCRYPTED,
                            sender_id=self._identity.peer_id,
                            recipient_id=pkt.sender_id,
                            payload=receipt_wire,
                            ttl=MESSAGE_TTL_HOPS,
                            timestamp=int(time.time() * 1000),
                        ))
                log.info("Delivery+read receipts sent to %s (file)", peer_hex)
            except Exception:
                log.warning("Could not send file receipts to %s", peer_hex)
            return

        # Text message
        msg_id, content = value
        log.info("DECRYPTED from %s  content=%r", peer_hex, content)
        self._publish({"event": "message", "from": peer_hex,
                       "nick": self._peers.get(peer_hex, ""),
                       "content": content, "private": True, "self": False,
                       "ts": int(time.time()), "msg_id": msg_id})

        try:
            for receipt_byte in (0x03, 0x02):
                receipt_wire = sess.encrypt(bytes([receipt_byte]) + msg_id.encode())
                if receipt_wire:
                    self._send_packet(BitchatPacket(
                        type=MessageType.NOISE_ENCRYPTED,
                        sender_id=self._identity.peer_id,
                        recipient_id=pkt.sender_id,
                        payload=receipt_wire,
                        ttl=MESSAGE_TTL_HOPS,
                        timestamp=int(time.time() * 1000),
                    ))
            log.info("Delivery+read receipts sent to %s", peer_hex)
        except Exception:
            log.warning("Could not send receipts to %s", peer_hex)

    def _handle_file_transfer(self, pkt: BitchatPacket, peer_hex: str) -> None:
        data   = pkt.payload
        actual = data
        for off in range(min(32, len(data))):
            if _sniff_file(data[off:])[1] != "application/octet-stream":
                actual = data[off:]
                break
        msg_id = str(uuid.uuid4())
        path, mime = _save_received_file(msg_id, actual)
        name = f"file{_sniff_file(actual)[0]}"
        log.info("FILE_TRANSFER from %s  mime=%s  %d bytes  saved=%s",
                 peer_hex, mime, len(actual), path)
        self._publish({"event": "file", "from": peer_hex,
                       "nick": self._peers.get(peer_hex, ""),
                       "path": path, "mime": mime, "name": name})

    def _handle_public_message(self, pkt: BitchatPacket, peer_hex: str) -> None:
        try:
            content = pkt.payload.decode("utf-8")
        except Exception:
            content = pkt.payload.hex()
        log.info("MESSAGE from %s  content=%r", peer_hex, content)
        msg_id = str(uuid.uuid4())
        self._publish({"event": "message", "from": peer_hex,
                       "nick": self._peers.get(peer_hex, ""),
                       "content": content, "private": False, "self": False,
                       "ts": int(time.time()), "msg_id": msg_id})

    def _handle_request_sync(self, pkt: BitchatPacket, peer_hex: str) -> None:
        log.debug("REQUEST_SYNC from %s", peer_hex)
        now = time.time()
        if now - self._last_announced.get(peer_hex, 0) >= _ANNOUNCE_COOLDOWN:
            self._send_packet(self.make_announce())
            self._last_announced[peer_hex] = now

    def _handle_leave(self, pkt: BitchatPacket, peer_hex: str) -> None:
        log.info("LEAVE from %s", peer_hex)
        nick = self._peers.pop(peer_hex, peer_hex[:8])
        self._sessions.pop(peer_hex, None)
        self._pending_msg2.pop(peer_hex, None)
        self._last_msg1.pop(peer_hex, None)
        self._session_timestamps.pop(peer_hex, None)
        self._peer_noise_pub.pop(peer_hex, None)
        self._last_announced.pop(peer_hex, None)
        self._pending_sends.pop(peer_hex, None)
        self._publish({"event": "peer", "action": "lost",
                       "peer_id": peer_hex, "nick": nick})

    # ── IPC command handler ────────────────────────────────────────────────────

    async def handle_command(self, cmd: dict) -> dict | None:
        name = cmd.get("cmd")

        if name == "ping":
            return {"ok": True, "pong": True}

        if name == "peers":
            return {"ok": True, "event": "peers",
                    "list": [{"peer_id": k, "nick": v,
                              "last_seen": self._peer_last_seen.get(k, 0.0)}
                             for k, v in self._peers.items()]}

        if name == "set_nick":
            nick = cmd.get("nick", "").strip()
            if not nick:
                return {"ok": False, "error": "invalid_params"}
            self._nickname = nick
            self._last_announced.clear()  # reset cooldowns so next peer echo uses new nick
            self._send_packet(self.make_announce())
            log.info("IPC set_nick → %r", nick)
            return {"ok": True, "nick": nick}

        if name == "send":
            to_hex  = cmd.get("to", "")
            content = cmd.get("content", "")
            if not to_hex or not content:
                return {"ok": False, "error": "invalid_params"}
            sess = self._sessions.get(to_hex)
            if sess is None or not sess.is_established:
                msg_id = str(uuid.uuid4())
                self._pending_sends.setdefault(to_hex, []).append(
                    (content, msg_id, time.time()))
                self._last_announced.pop(to_hex, None)
                self._send_packet(self.make_announce())
                self._last_announced[to_hex] = time.time()
                log.info("No session for %s — queued message, sent ANNOUNCE invite",
                         to_hex[:12])
                return {"ok": True, "msg_id": msg_id}
            try:
                recipient_id = bytes.fromhex(to_hex)
            except ValueError:
                return {"ok": False, "error": "invalid_peer_id"}
            payload, msg_id = _encode_message_payload(content)
            wire = sess.encrypt(payload)
            if wire is None:
                return {"ok": False, "error": "encrypt_failed"}
            self._send_packet(BitchatPacket(
                type=MessageType.NOISE_ENCRYPTED,
                sender_id=self._identity.peer_id,
                recipient_id=recipient_id,
                payload=wire,
                ttl=MESSAGE_TTL_HOPS,
                timestamp=int(time.time() * 1000),
            ))
            self._publish({"event": "message",
                           "from": self._identity.peer_id.hex(),
                           "nick": self._nickname,
                           "content": content, "private": True, "self": True,
                           "ts": int(time.time()), "msg_id": msg_id})
            log.info("IPC send→%s  content=%r", to_hex[:12], content)
            return {"ok": True, "msg_id": msg_id}

        if name == "broadcast":
            content = cmd.get("content", "")
            if not content:
                return {"ok": False, "error": "invalid_params"}
            msg_id = str(uuid.uuid4())
            self._send_packet(self.make_message_packet(content))
            self._publish({"event": "message",
                           "from": self._identity.peer_id.hex(),
                           "nick": self._nickname,
                           "content": content, "private": False, "self": True,
                           "ts": int(time.time()), "msg_id": msg_id})
            log.info("IPC broadcast  content=%r", content)
            return {"ok": True, "msg_id": msg_id}

        if name == "send_file":
            to_hex    = cmd.get("to", "")
            file_path = cmd.get("path", "")
            if not to_hex or not file_path:
                return {"ok": False, "error": "invalid_params"}
            try:
                file_data = Path(file_path).read_bytes()
            except Exception as exc:
                return {"ok": False, "error": "file_read_error", "detail": str(exc)}
            sess = self._sessions.get(to_hex)
            if sess is None or not sess.is_established:
                return {"ok": False, "error": "no_session"}
            try:
                recipient_id = bytes.fromhex(to_hex)
            except ValueError:
                return {"ok": False, "error": "invalid_peer_id"}
            payload, msg_id = _encode_file_payload(file_data)
            wire = sess.encrypt(payload)
            if wire is None:
                return {"ok": False, "error": "encrypt_failed"}
            self._fragment_and_send(BitchatPacket(
                type=MessageType.NOISE_ENCRYPTED,
                sender_id=self._identity.peer_id,
                recipient_id=recipient_id,
                payload=wire,
                ttl=MESSAGE_TTL_HOPS,
                timestamp=int(time.time() * 1000),
            ))
            log.info("IPC send_file→%s  path=%s  %d bytes",
                     to_hex[:12], file_path, len(file_data))
            return {"ok": True, "msg_id": msg_id}

        return {"ok": False, "error": "unknown_command"}
