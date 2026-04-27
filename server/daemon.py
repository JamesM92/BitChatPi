#!/usr/bin/env python3
"""
BitChatPi daemon — Pi acts as a BitChat peer (peripheral/responder).

IPC API (Unix socket, newline-delimited JSON):
  Send commands:  {"cmd":"send","to":"<peer_id_hex>","content":"..."}
                  {"cmd":"broadcast","content":"...","channel":"..."}
                  {"cmd":"peers"}
  Receive events: {"event":"message","from":"...","nick":"...","content":"...","private":true}
                  {"event":"peer","action":"seen"|"lost","peer_id":"...","nick":"..."}
                  {"event":"receipt","type":"delivery"|"read","ref":"...","from":"..."}

Usage:
    sudo .venv/bin/python3 server/daemon.py
"""
import asyncio
import logging
import struct
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bitchatd.protocol.constants import (
    SERVICE_UUID, CHARACTERISTIC_UUID,
    MessageType, MESSAGE_TTL_HOPS, BROADCAST_ID,
)
from bitchatd.mesh.relay_engine import RelayEngine
from bitchatd.mesh.fragment_manager import FragmentManager
from bitchatd.protocol.packet import BitchatPacket
from bitchatd.protocol.codec import encode, decode
from bitchatd.protocol.announce import encode_announce, decode_announce
from bitchatd.crypto.identity import load_or_create
from bitchatd.crypto.noise_session import NoiseSession
from bitchatd.crypto.signing import sign
from bitchatd.ble.gatt_server import GattServer
from bitchatd.ble.advertise import start_le_advertisement, stop_le_advertisement
from bitchatd.api import IpcServer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("dissononce").setLevel(logging.WARNING)
log = logging.getLogger("smoke")

NICKNAME      = "BitChatPi"
IDENTITY_PATH = Path.home() / ".config" / "bitchatd" / "identity.json"
IPC_SOCK_PATH = str(Path.home() / ".config" / "bitchatd" / "api.sock")

# Per-peer Noise sessions: peer_id_hex → NoiseSession
_sessions: dict[str, NoiseSession] = {}
_gatt:         GattServer | None = None
_ipc:          IpcServer | None = None
_identity      = None          # set in run()
# Last time we sent an ANNOUNCE to each peer (rate-limit echoes)
_last_announced: dict[str, float] = {}
_ANNOUNCE_COOLDOWN = 10.0  # seconds
# Known peers: peer_id_hex → nickname
_peers: dict[str, str] = {}
# Handshake retry state: cached msg1 bytes and msg2 bytes per peer
# Used to re-send msg2 when the phone duplicates msg1 before receiving our reply.
_last_msg1:     dict[str, bytes] = {}
_pending_msg2:  dict[str, bytes] = {}
# Relay and fragment reassembly engines
_relay:         RelayEngine | None = None
_fragments:     FragmentManager | None = None
# Messages queued while waiting for a Noise session to be established.
# Stores (content, msg_id) so the same UUID is used when the message is finally sent.
_pending_sends: dict[str, list[tuple[str, str]]] = {}
# Time each session was created/reset, used to detect stale in-progress sessions
_session_timestamps: dict[str, float] = {}


def _publish(event: dict) -> None:
    """Schedule an IPC fan-out without blocking the sync packet handler."""
    if _ipc is not None:
        asyncio.ensure_future(_ipc.publish(event))


def _encode_message_payload(content: str, msg_id: str | None = None) -> tuple[bytes, str]:
    """Return (wire_bytes, msg_id).  Caller uses msg_id to correlate receipts."""
    # flags(1) | id_len(uint16) | id | msg_type(1)=1 | content_len(uint8) | content
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
    # flags(1) | id_len(uint16 BE) | id(36) | msg_type(1)=2 | raw file bytes
    msg_id = str(uuid.uuid4())
    id_b = msg_id.encode()
    out = bytearray()
    out += struct.pack(">B", 0x00)       # flags
    out += struct.pack(">H", len(id_b))  # id_len = 36
    out += id_b
    out += struct.pack(">B", 2)          # msg_type = 2 (binary/file)
    out += data
    return bytes(out), msg_id


# Magic-byte → (ext, mime) table for received file sniffing
_FILE_MAGIC: list[tuple[bytes, str, str]] = [
    (b'\xff\xd8\xff',           ".jpg",  "image/jpeg"),
    (b'\x89PNG\r\n\x1a\n',     ".png",  "image/png"),
    (b'GIF87a',                 ".gif",  "image/gif"),
    (b'GIF89a',                 ".gif",  "image/gif"),
    (b'RIFF',                   ".webp", "image/webp"),   # checked below for WEBP
    (b'\x00\x00\x00\x0cftyp',  ".mp4",  "video/mp4"),
    (b'ID3',                    ".mp3",  "audio/mpeg"),
    (b'\xff\xfb',               ".mp3",  "audio/mpeg"),
    (b'OggS',                   ".ogg",  "audio/ogg"),
    (b'fLaC',                   ".flac", "audio/flac"),
]


def _sniff_file(data: bytes) -> tuple[str, str]:
    """Return (ext, mime) by inspecting magic bytes, defaulting to .bin."""
    for magic, ext, mime in _FILE_MAGIC:
        if data[:len(magic)] == magic:
            if magic == b'RIFF' and len(data) >= 12 and data[8:12] == b'WEBP':
                return ext, mime
            elif magic != b'RIFF':
                return ext, mime
    return ".bin", "application/octet-stream"


def _save_received_file(msg_id: str, data: bytes) -> tuple[str, str]:
    """Save binary data to /tmp/, return (path, mime)."""
    ext, mime = _sniff_file(data)
    path = f"/tmp/bitchat_{msg_id[:8]}{ext}"
    with open(path, "wb") as fh:
        fh.write(data)
    return path, mime


def _parse_incoming(plaintext: bytes) -> tuple[str, object] | None:
    """
    Parse a decrypted NOISE_ENCRYPTED inner payload.
    Returns ('receipt', ref_uuid_str) for delivery/read receipts.
    Returns ('message', (msg_id, content_str)) for text messages.
    Returns ('file', (msg_id, path, mime, name)) for binary file/image/audio.
    Returns None if completely unrecognisable.
    """
    # Receipt: exactly 37 bytes, first byte 0x02 (read) or 0x03 (delivery)
    if len(plaintext) == 37 and plaintext[0] in (0x02, 0x03):
        try:
            return ('receipt', plaintext[1:37].decode('ascii'))
        except Exception:
            pass

    # Observed framing: flags(1) | id_len(uint16 BE)(2) | id(36) | msg_type(1) | ...
    if len(plaintext) >= 41:
        try:
            id_len = struct.unpack_from(">H", plaintext, 1)[0]
            if id_len == 36:
                msg_id   = plaintext[3:39].decode('ascii')
                msg_type = plaintext[39]
                rest     = plaintext[40:]

                if msg_type == 1:
                    # Text: content_len(uint8) | utf-8 text
                    if rest:
                        cont_len = rest[0]
                        content  = rest[1:1 + cont_len].decode('utf-8')
                        return ('message', (msg_id, content))
                else:
                    # Binary: scan for magic bytes, falling back to raw
                    log.debug("NOISE binary msg_type=0x%02x  %d bytes", msg_type, len(rest))
                    data = rest
                    for off in (0, 1, 4, 5, 8, 9):
                        if off < len(rest) and _sniff_file(rest[off:])[1] != "application/octet-stream":
                            data = rest[off:]
                            break
                    path, mime = _save_received_file(msg_id, data)
                    name = f"file{_sniff_file(data)[0]}"
                    return ('file', (msg_id, path, mime, name))
        except Exception:
            pass

    # FILE_TRANSFER TLV format (observed 2026-04-26):
    #   magic(2)=0x2001 | TLV fields:
    #     tag=0x00, len(2), filename
    #     tag=0x02, len(2)=4, file_size(4)
    #     tag=0x03, len(2), mime_type
    #     tag=0x04, len(4), file_data
    if len(plaintext) >= 7 and plaintext[0] == 0x20 and plaintext[1] == 0x01:
        try:
            off = 2
            fname = mime_type = b""
            file_data = b""
            while off + 3 <= len(plaintext):
                tag = plaintext[off]; off += 1
                if tag == 0x04:
                    # 4-byte length for the actual file data
                    if off + 4 > len(plaintext): break
                    dlen = struct.unpack_from(">I", plaintext, off)[0]; off += 4
                    file_data = plaintext[off:off + dlen]
                    break
                flen = struct.unpack_from(">H", plaintext, off)[0]; off += 2
                val = plaintext[off:off + flen]; off += flen
                if tag == 0x00: fname = val
                elif tag == 0x03: mime_type = val
            if file_data:
                msg_id = str(uuid.uuid4())
                # prefer detected extension from magic bytes
                detected_ext, detected_mime = _sniff_file(file_data)
                actual_mime = mime_type.decode(errors="replace") if mime_type else detected_mime
                path, _ = _save_received_file(msg_id, file_data)
                name = fname.decode(errors="replace") if fname else f"file{detected_ext}"
                log.info("FILE_TRANSFER TLV: name=%s  mime=%s  %d bytes", name, actual_mime, len(file_data))
                return ('file', (msg_id, path, actual_mime, name))
        except Exception:
            pass

    # Stale BitchatMessage format: flags(1) | timestamp(8) | id_len(uint8) | id | sender_len(1) | sender | content_len(uint16) | content
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
                    content_data = plaintext[content_start + 2: content_start + 2 + content_len]
                    if content_data:
                        try:
                            return ('message', (msg_id, content_data.decode('utf-8')))
                        except UnicodeDecodeError:
                            # Binary content — sniff and save
                            data = content_data
                            for off in (0, 1, 4, 8):
                                if off < len(data) and _sniff_file(data[off:])[1] != "application/octet-stream":
                                    data = data[off:]
                                    break
                            path, mime = _save_received_file(msg_id, data)
                            name = f"file{_sniff_file(data)[0]}"
                            return ('file', (msg_id, path, mime, name))
        except Exception:
            pass

    # Last-resort: scan the entire plaintext for a known file signature
    if len(plaintext) > 64:
        for off in range(min(64, len(plaintext) - 4)):
            ext, mime = _sniff_file(plaintext[off:])
            if mime != "application/octet-stream":
                msg_id = str(uuid.uuid4())
                path, _ = _save_received_file(msg_id, plaintext[off:])
                name = f"file{ext}"
                log.info("Raw file detected at offset %d  mime=%s", off, mime)
                return ('file', (msg_id, path, mime, name))

    return None


def _send_packet(pkt: BitchatPacket) -> None:
    raw = encode(pkt, pad=True)
    if raw is None:
        log.warning("Failed to encode packet")
        return
    if _gatt is None:
        log.warning("GATT server not ready")
        return
    _gatt.send(raw)
    log.info("SENT type=0x%02x  %d bytes", pkt.type, len(raw))


def _make_announce() -> BitchatPacket:
    payload = encode_announce(
        NICKNAME,
        _identity.noise_keypair.public.data,
        _identity.sign_public,
    )
    unsigned = BitchatPacket(
        type=MessageType.ANNOUNCE,
        sender_id=_identity.peer_id,
        payload=payload,
        ttl=0,
        timestamp=int(time.time() * 1000),
    )
    sig_data = encode(unsigned, pad=True)
    sig = sign(_identity.sign_private, sig_data) if sig_data else None

    return BitchatPacket(
        type=MessageType.ANNOUNCE,
        sender_id=_identity.peer_id,
        payload=payload,
        ttl=MESSAGE_TTL_HOPS,
        timestamp=unsigned.timestamp,
        signature=sig,
    )


def _make_message_packet(content: str) -> BitchatPacket:
    """Build a signed public MESSAGE packet (raw UTF-8 payload, TTL=0 for signing)."""
    ts = int(time.time() * 1000)
    payload = content.encode("utf-8")
    unsigned = BitchatPacket(
        type=MessageType.MESSAGE,
        sender_id=_identity.peer_id,
        payload=payload,
        ttl=0,
        timestamp=ts,
    )
    sig_data = encode(unsigned, pad=True)
    sig = sign(_identity.sign_private, sig_data) if sig_data else None
    return BitchatPacket(
        type=MessageType.MESSAGE,
        sender_id=_identity.peer_id,
        payload=payload,
        ttl=MESSAGE_TTL_HOPS,
        timestamp=ts,
        signature=sig,
    )


def _handle_packet(pkt: BitchatPacket) -> None:
    peer_hex = pkt.sender_id.hex()

    if pkt.type == MessageType.ANNOUNCE:
        ann = decode_announce(pkt.payload)
        if ann:
            log.info("ANNOUNCE from %s  nick=%s  noise_pub=%s…",
                     peer_hex, ann.nickname, ann.noise_pub.hex()[:12])
            if _peers.get(peer_hex) != ann.nickname:
                _peers[peer_hex] = ann.nickname
                _publish({"event": "peer", "action": "seen",
                          "peer_id": peer_hex, "nick": ann.nickname})
        else:
            log.warning("Malformed ANNOUNCE from %s", peer_hex)

        now = time.time()
        if now - _last_announced.get(peer_hex, 0) >= _ANNOUNCE_COOLDOWN:
            _send_packet(_make_announce())
            _last_announced[peer_hex] = now
        else:
            log.debug("ANNOUNCE cooldown for %s — skipping echo", peer_hex)

        existing = _sessions.get(peer_hex)
        if existing is None:
            sess = NoiseSession.create_responder(_identity.noise_keypair)
            _sessions[peer_hex] = sess
            _session_timestamps[peer_hex] = time.time()
            log.info("Noise responder session ready for %s", peer_hex)
        elif existing.is_established:
            log.info("Keeping existing established session for %s", peer_hex)
        else:
            # In-progress (never established). If it's been > 30 s the phone has
            # moved on — reset so the next handshake attempt starts fresh.
            age = time.time() - _session_timestamps.get(peer_hex, 0)
            if age > 30:
                log.info("Resetting stale in-progress session (age=%.0fs) for %s", age, peer_hex)
                sess = NoiseSession.create_responder(_identity.noise_keypair)
                _sessions[peer_hex] = sess
                _session_timestamps[peer_hex] = time.time()
                _pending_msg2.pop(peer_hex, None)
                _last_msg1.pop(peer_hex, None)
                _last_announced.pop(peer_hex, None)  # bypass cooldown on next announce
            else:
                log.info("Keeping recent in-progress session (age=%.0fs) for %s", age, peer_hex)

    elif pkt.type == MessageType.NOISE_HANDSHAKE:
        sess = _sessions.get(peer_hex)
        if sess is None:
            log.warning("NOISE_HANDSHAKE from unknown peer %s — ignoring", peer_hex)
            return

        log.info("NOISE_HANDSHAKE from %s  %d bytes", peer_hex, len(pkt.payload))

        # Handle duplicate or new msg1 (32 bytes) on a non-established session.
        if not sess.is_established and len(pkt.payload) == 32:
            cached_msg2 = _pending_msg2.get(peer_hex)
            last_m1     = _last_msg1.get(peer_hex)
            if last_m1 == pkt.payload and cached_msg2 is not None:
                # Same msg1 bytes → phone retried before receiving our msg2.
                # Re-send the cached msg2; the session state is already correct.
                log.info("Phone retried msg1 for %s — re-sending cached msg2", peer_hex)
                _send_packet(BitchatPacket(
                    type=MessageType.NOISE_HANDSHAKE,
                    sender_id=_identity.peer_id,
                    recipient_id=pkt.sender_id,
                    payload=cached_msg2,
                    ttl=MESSAGE_TTL_HOPS,
                    timestamp=int(time.time() * 1000),
                ))
                return
            else:
                # Different msg1 bytes → phone started a fresh handshake; reset.
                if last_m1 is not None:
                    log.info("Phone sent new msg1 for %s — resetting session", peer_hex)
                sess = NoiseSession.create_responder(_identity.noise_keypair)
                _sessions[peer_hex] = sess
                _pending_msg2.pop(peer_hex, None)
                _last_msg1.pop(peer_hex, None)

        # Read the incoming handshake message.
        if sess.read_handshake_message(pkt.payload) is None:
            log.error("Handshake read failed for %s", peer_hex)
            del _sessions[peer_hex]
            _pending_msg2.pop(peer_hex, None)
            _last_msg1.pop(peer_hex, None)
            return

        # Track msg1 bytes for retry detection.
        if len(pkt.payload) == 32:
            _last_msg1[peer_hex] = pkt.payload

        # msg3 for responder → session established, no reply needed.
        if sess.is_established:
            log.info("Noise session ESTABLISHED with %s  remote_pub=%s…",
                     peer_hex, (sess.remote_static_public or b'').hex()[:12])
            _pending_msg2.pop(peer_hex, None)
            _last_msg1.pop(peer_hex, None)
            # Drain any messages that were queued before the session existed.
            queued = _pending_sends.pop(peer_hex, [])
            if queued:
                try:
                    recipient_id = bytes.fromhex(peer_hex)
                except ValueError:
                    pass
                else:
                    for content, msg_id in queued:
                        payload, _ = _encode_message_payload(content, msg_id)
                        wire = sess.encrypt(payload)
                        if wire:
                            _send_packet(BitchatPacket(
                                type=MessageType.NOISE_ENCRYPTED,
                                sender_id=_identity.peer_id,
                                recipient_id=recipient_id,
                                payload=wire,
                                ttl=MESSAGE_TTL_HOPS,
                                timestamp=int(time.time() * 1000),
                            ))
                            log.info("Delivered queued message to %s: %r",
                                     peer_hex[:12], content)
            return

        # msg1 received → write msg2 and cache it for potential retries.
        # Refresh timestamp so the 30-s stale reset doesn't fire mid-handshake.
        _session_timestamps[peer_hex] = time.time()
        outgoing = sess.write_handshake_message()
        if outgoing is None:
            log.error("Handshake write failed for %s", peer_hex)
            del _sessions[peer_hex]
            _pending_msg2.pop(peer_hex, None)
            _last_msg1.pop(peer_hex, None)
            return

        _pending_msg2[peer_hex] = outgoing
        _send_packet(BitchatPacket(
            type=MessageType.NOISE_HANDSHAKE,
            sender_id=_identity.peer_id,
            recipient_id=pkt.sender_id,
            payload=outgoing,
            ttl=MESSAGE_TTL_HOPS,
            timestamp=int(time.time() * 1000),
        ))
        log.info("Noise msg2 sent to %s  %d bytes", peer_hex, len(outgoing))

    elif pkt.type == MessageType.NOISE_ENCRYPTED:
        sess = _sessions.get(peer_hex)
        if sess is None or not sess.is_established:
            # Phone has stale/old keys or Pi restarted. Reset the session and send
            # our ANNOUNCE immediately so the phone knows to re-initiate the handshake.
            _sessions[peer_hex] = NoiseSession.create_responder(_identity.noise_keypair)
            _last_announced.pop(peer_hex, None)
            _send_packet(_make_announce())
            _last_announced[peer_hex] = time.time()
            log.warning("NOISE_ENCRYPTED from %s with no/stale session — sent ANNOUNCE to prompt re-handshake", peer_hex)
            return
        plaintext = sess.decrypt(pkt.payload)
        if plaintext is None:
            log.warning("Decrypt failed from %s", peer_hex)
            return

        parsed = _parse_incoming(plaintext)
        if parsed is None:
            log.warning("Unknown NOISE_ENCRYPTED format from %s  len=%d  hex=%s…",
                        peer_hex, len(plaintext), plaintext[:64].hex())
            return

        kind, value = parsed
        if kind == 'receipt':
            receipt_type = "read" if plaintext[0] == 0x02 else "delivery"
            log.info("RECEIPT %s from %s  ref=%s…", receipt_type, peer_hex, value[:8])
            _publish({"event": "receipt", "type": receipt_type,
                      "ref": value, "from": peer_hex})
            return

        if kind == 'file':
            msg_id, path, mime, name = value
            log.info("FILE from %s  mime=%s  saved=%s", peer_hex, mime, path)
            _publish({"event": "file", "from": peer_hex,
                      "nick": _peers.get(peer_hex, peer_hex[:8]),
                      "path": path, "mime": mime, "name": name})
            # Send delivery receipt for files too
            try:
                receipt_wire = sess.encrypt(bytes([0x03]) + msg_id.encode())
                if receipt_wire:
                    _send_packet(BitchatPacket(
                        type=MessageType.NOISE_ENCRYPTED,
                        sender_id=_identity.peer_id,
                        recipient_id=pkt.sender_id,
                        payload=receipt_wire,
                        ttl=MESSAGE_TTL_HOPS,
                        timestamp=int(time.time() * 1000),
                    ))
            except Exception:
                pass
            return

        # Text message
        msg_id, content = value
        log.info("DECRYPTED from %s  content=%r", peer_hex, content)
        _publish({"event": "message", "from": peer_hex,
                  "nick": _peers.get(peer_hex, peer_hex[:8]),
                  "content": content, "private": True})

        # Delivery receipt (0x03) + read receipt (0x02) — Pi shows all messages immediately
        try:
            for receipt_type in (0x03, 0x02):
                receipt_wire = sess.encrypt(bytes([receipt_type]) + msg_id.encode())
                if receipt_wire:
                    _send_packet(BitchatPacket(
                        type=MessageType.NOISE_ENCRYPTED,
                        sender_id=_identity.peer_id,
                        recipient_id=pkt.sender_id,
                        payload=receipt_wire,
                        ttl=MESSAGE_TTL_HOPS,
                        timestamp=int(time.time() * 1000),
                    ))
            log.info("Delivery+read receipts sent to %s", peer_hex)
        except Exception:
            log.warning("Could not send receipts to %s", peer_hex)


    elif pkt.type == MessageType.FILE_TRANSFER:
        # Reassembled file packet (after fragment reassembly).
        # Payload is raw binary — sniff format and save.
        data = pkt.payload
        actual = data
        for off in (0, 1, 4, 8):
            if off < len(data) and _sniff_file(data[off:])[1] != "application/octet-stream":
                actual = data[off:]
                break
        msg_id = str(uuid.uuid4())
        path, mime = _save_received_file(msg_id, actual)
        name = f"file{_sniff_file(actual)[0]}"
        log.info("FILE_TRANSFER from %s  mime=%s  %d bytes  saved=%s",
                 peer_hex, mime, len(actual), path)
        _publish({"event": "file", "from": peer_hex,
                  "nick": _peers.get(peer_hex, peer_hex[:8]),
                  "path": path, "mime": mime, "name": name})

    elif pkt.type == MessageType.MESSAGE:
        try:
            content = pkt.payload.decode("utf-8")
        except Exception:
            content = pkt.payload.hex()
        log.info("MESSAGE from %s  content=%r", peer_hex, content)
        _publish({"event": "message", "from": peer_hex,
                  "nick": _peers.get(peer_hex, peer_hex[:8]),
                  "content": content, "private": False})

    else:
        log.info("PKT type=0x%02x  ttl=%d  from=%s  %d bytes payload",
                 pkt.type, pkt.ttl, peer_hex, len(pkt.payload))


async def _relay_broadcast(pkt: BitchatPacket, from_peer_id: str) -> None:
    """RelayEngine callback: re-broadcast a relayed packet via GATT notify."""
    raw = encode(pkt, pad=True)
    if raw and _gatt:
        _gatt.send(raw)
        log.debug("RELAY type=0x%02x  ttl=%d  from=%s  %d bytes",
                  pkt.type, pkt.ttl, from_peer_id[:8], len(raw))


def _schedule_relay(pkt: BitchatPacket, from_peer_hex: str) -> None:
    if _relay is not None:
        asyncio.ensure_future(_relay.handle_relay(pkt, from_peer_hex))


def _dispatch(pkt: BitchatPacket) -> None:
    """Route an incoming packet: process locally if for us, then relay if appropriate."""
    # Ignore packets we sent (prevents relay loops via GATT loopback)
    if _identity and pkt.sender_id == _identity.peer_id:
        return

    peer_hex = pkt.sender_id.hex()

    # FRAGMENT: try reassembly (sync), then relay this hop regardless
    if pkt.type == MessageType.FRAGMENT:
        if _fragments is not None:
            reassembled = _fragments.handle_fragment(pkt)
            if reassembled is not None:
                log.info("FRAGMENT reassembled from %s  original_type=0x%02x  %d bytes",
                         peer_hex, reassembled.type, len(reassembled.payload))
                _dispatch(reassembled)  # reassembled TTL=0, won't relay again
        _schedule_relay(pkt, peer_hex)
        return

    # Decide local processing: broadcasts and packets addressed directly to us
    is_for_me = pkt.is_broadcast or (
        _identity is not None and
        pkt.recipient_id is not None and
        pkt.recipient_id == _identity.peer_id
    )
    if is_for_me:
        _handle_packet(pkt)

    # Relay: anything that isn't exclusively addressed to us
    is_unicast_to_me = (
        _identity is not None and
        pkt.recipient_id is not None and
        not pkt.is_broadcast and
        pkt.recipient_id == _identity.peer_id
    )
    if not is_unicast_to_me:
        _schedule_relay(pkt, peer_hex)


def _on_ble_write(data: bytes, device_path: str | None = None) -> None:
    log.debug("WRITE  %d raw bytes  device=%s", len(data), device_path)
    pkt = decode(data)
    if pkt is None:
        log.warning("Could not decode incoming packet (%d bytes): %s",
                    len(data), data.hex()[:32])
        return
    _dispatch(pkt)


async def _ipc_command_handler(cmd: dict) -> dict | None:
    name = cmd.get("cmd")

    if name == "peers":
        return {"ok": True, "event": "peers",
                "list": [{"peer_id": k, "nick": v} for k, v in _peers.items()]}

    if name == "send":
        to_hex = cmd.get("to", "")
        content = cmd.get("content", "")
        if not to_hex or not content:
            return {"ok": False, "error": "missing 'to' or 'content'"}
        sess = _sessions.get(to_hex)
        if sess is None or not sess.is_established:
            # No session yet — queue the message and send our ANNOUNCE so the
            # phone knows to initiate the Noise handshake.
            msg_id = str(uuid.uuid4())
            _pending_sends.setdefault(to_hex, []).append((content, msg_id))
            _last_announced.pop(to_hex, None)   # bypass cooldown for explicit invite
            _send_packet(_make_announce())
            _last_announced[to_hex] = time.time()
            log.info("No session for %s — queued message, sent ANNOUNCE invite", to_hex[:12])
            return {"ok": True, "msg_id": msg_id}
        try:
            recipient_id = bytes.fromhex(to_hex)
        except ValueError:
            return {"ok": False, "error": "invalid peer_id hex"}
        payload, msg_id = _encode_message_payload(content)
        wire = sess.encrypt(payload)
        if wire is None:
            return {"ok": False, "error": "encryption failed"}
        _send_packet(BitchatPacket(
            type=MessageType.NOISE_ENCRYPTED,
            sender_id=_identity.peer_id,
            recipient_id=recipient_id,
            payload=wire,
            ttl=MESSAGE_TTL_HOPS,
            timestamp=int(time.time() * 1000),
        ))
        log.info("IPC send→%s  content=%r", to_hex[:12], content)
        return {"ok": True, "msg_id": msg_id}

    if name == "broadcast":
        content = cmd.get("content", "")
        if not content:
            return {"ok": False, "error": "missing 'content'"}
        _send_packet(_make_message_packet(content))
        log.info("IPC broadcast  content=%r", content)
        return {"ok": True}

    if name == "send_file":
        to_hex = cmd.get("to", "")
        file_path = cmd.get("path", "")
        if not to_hex or not file_path:
            return {"ok": False, "error": "missing 'to' or 'path'"}
        try:
            file_data = Path(file_path).read_bytes()
        except Exception as exc:
            return {"ok": False, "error": f"cannot read file: {exc}"}
        sess = _sessions.get(to_hex)
        if sess is None or not sess.is_established:
            return {"ok": False, "error": "no established session — send a DM first"}
        try:
            recipient_id = bytes.fromhex(to_hex)
        except ValueError:
            return {"ok": False, "error": "invalid peer_id hex"}
        payload, msg_id = _encode_file_payload(file_data)
        wire = sess.encrypt(payload)
        if wire is None:
            return {"ok": False, "error": "encryption failed"}
        pkt = BitchatPacket(
            type=MessageType.NOISE_ENCRYPTED,
            sender_id=_identity.peer_id,
            recipient_id=recipient_id,
            payload=wire,
            ttl=MESSAGE_TTL_HOPS,
            timestamp=int(time.time() * 1000),
        )
        if _fragments:
            for frag in _fragments.create_fragments(pkt):
                _send_packet(frag)
        else:
            _send_packet(pkt)
        log.info("IPC send_file→%s  path=%s  %d bytes", to_hex[:12], file_path, len(file_data))
        return {"ok": True, "msg_id": msg_id}

    return {"ok": False, "error": f"unknown command: {name!r}"}


async def _ensure_adapter_up() -> None:
    """Wait for the BLE adapter to report Powered: yes (up to 15 s with retries).

    After `systemctl restart bluetooth` the BCM43455 UART chip on the Pi can
    take several seconds after hci0 shows UP before LE advertising is usable.
    Checking bluetoothctl Powered: yes is the reliable readiness signal.
    """
    for attempt in range(15):
        proc = await asyncio.create_subprocess_exec(
            "bluetoothctl", "show",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        if b"Powered: yes" in out:
            if attempt > 0:
                log.info("BLE adapter ready after %d s", attempt)
            return
        if attempt == 0:
            log.warning("BLE adapter not powered — sending power on")
            await asyncio.create_subprocess_exec(
                "bluetoothctl", "power", "on",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
        await asyncio.sleep(1)
    log.error("BLE adapter not ready after 15 s — advertising may fail")


async def run():
    global _gatt, _ipc, _identity, _relay, _fragments

    await _ensure_adapter_up()

    _identity = load_or_create(IDENTITY_PATH)
    log.info("peer_id=%s  nick=%s", _identity.peer_id.hex(), NICKNAME)

    _relay = RelayEngine(_identity.peer_id)
    _relay.broadcast_packet = _relay_broadcast
    _relay.get_network_size = lambda: max(1, len(_peers))

    _fragments = FragmentManager()
    _fragments.start()

    _gatt = GattServer()
    await _gatt.start(SERVICE_UUID, CHARACTERISTIC_UUID, _on_ble_write)
    log.info("GATT server registered")

    _ipc = IpcServer(IPC_SOCK_PATH)
    _ipc.set_command_handler(_ipc_command_handler)
    await _ipc.start()

    adv_bus = await start_le_advertisement(SERVICE_UUID, NICKNAME)
    log.info("Advertising as '%s'  service=%s", NICKNAME, SERVICE_UUID)
    log.info("Running — press Ctrl-C to stop.")

    try:
        # Run indefinitely until interrupted
        while True:
            await asyncio.sleep(3600)
    finally:
        try:
            await stop_le_advertisement(adv_bus)
        except Exception:
            pass
        await _gatt.stop()
        await _ipc.stop()
        if _fragments:
            _fragments.stop()
        log.info("Done.")


if __name__ == "__main__":
    import signal

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    main_task = loop.create_task(run())

    def _shutdown(signum, frame):
        loop.call_soon_threadsafe(main_task.cancel)

    signal.signal(signal.SIGTERM, _shutdown)

    try:
        loop.run_until_complete(main_task)
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Interrupted.")
    finally:
        loop.close()
