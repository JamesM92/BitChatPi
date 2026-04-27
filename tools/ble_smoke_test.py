#!/usr/bin/env python3
"""
BLE GATT smoke test — Pi acts as a BitChat peer (peripheral/responder).

Flow:
  1. Advertises BitChat service UUID
  2. On ANNOUNCE received from phone → sends back our ANNOUNCE + creates Noise responder session
  3. On NOISE_HANDSHAKE msg1 → responds with msg2 (Noise XX responder)
  4. On NOISE_HANDSHAKE msg3 → session established
  5. On NOISE_ENCRYPTED → decrypts and prints payload

What you should see on the phone (BitChat):
  - Pi appears in peer list as "BitChatPi"
  - Private messages to BitChatPi are decryptable

Usage:
    sudo .venv/bin/python3 tools/ble_smoke_test.py
"""
import asyncio
import logging
import struct
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from bitchatd.protocol.constants import (
    SERVICE_UUID, CHARACTERISTIC_UUID,
    MessageType, MESSAGE_TTL_HOPS,
)
from bitchatd.protocol.packet import BitchatPacket
from bitchatd.protocol.codec import encode, decode
from bitchatd.protocol.announce import encode_announce, decode_announce
from bitchatd.crypto.identity import load_or_create
from bitchatd.crypto.noise_session import NoiseSession
from bitchatd.crypto.signing import sign
from bitchatd.ble.gatt_server import GattServer
from bitchatd.ble.advertise import start_le_advertisement, stop_le_advertisement
from bleak import BleakClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("dissononce").setLevel(logging.WARNING)
log = logging.getLogger("smoke")

NICKNAME      = "BitChatPi"
IDENTITY_PATH = Path.home() / ".config" / "bitchatd" / "identity.json"
RUN_SECONDS   = 120

# Per-peer Noise sessions: peer_id_hex → NoiseSession
_sessions: dict[str, NoiseSession] = {}
_gatt:         GattServer | None = None
_identity      = None          # set in run()
# BleakClient connections to phones' own GATT servers (reverse connections)
_peer_clients: dict[str, BleakClient] = {}   # device_path → client
_connecting:   set[str] = set()


def _encode_message_payload(content: str) -> bytes:
    # Wire format observed from phone: flags(1) | id_len(uint16) | id | msg_type(1)=1 | content_len(uint8) | content
    # flags=0x01 matches what the phone sends; no optional tail fields.
    msg_id    = str(uuid.uuid4())
    id_b      = msg_id.encode()
    content_b = content.encode()

    out = bytearray()
    out += struct.pack(">B", 0x01)            # flags — mirrors phone's observed value
    out += struct.pack(">H", len(id_b))       # id_len uint16
    out += id_b
    out += struct.pack(">B", 1)               # msg_type = 1 (text)
    out += struct.pack(">B", len(content_b))  # content_len uint8
    out += content_b
    return bytes(out)


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


def _handle_packet(pkt: BitchatPacket) -> None:
    peer_hex = pkt.sender_id.hex()

    if pkt.type == MessageType.ANNOUNCE:
        ann = decode_announce(pkt.payload)
        if ann:
            log.info("ANNOUNCE from %s  nick=%s  noise_pub=%s…",
                     peer_hex, ann.nickname, ann.noise_pub.hex()[:12])
        else:
            log.warning("Malformed ANNOUNCE from %s", peer_hex)

        _send_packet(_make_announce())

        existing = _sessions.get(peer_hex)
        if existing is None:
            sess = NoiseSession.create_responder(_identity.noise_keypair)
            _sessions[peer_hex] = sess
            log.info("Noise responder session ready for %s", peer_hex)
        else:
            log.info("Keeping existing %s session for %s",
                     "established" if existing.is_established else "in-progress", peer_hex)

    elif pkt.type == MessageType.NOISE_HANDSHAKE:
        sess = _sessions.get(peer_hex)
        if sess is None:
            log.warning("NOISE_HANDSHAKE from unknown peer %s — ignoring", peer_hex)
            return

        log.info("NOISE_HANDSHAKE from %s  %d bytes", peer_hex, len(pkt.payload))

        # Phone retried msg1 (32 bytes) while our session is still in-progress —
        # reset so we can process the fresh msg1 rather than failing on a corrupt state.
        if not sess.is_established and len(pkt.payload) == 32:
            log.info("Phone retried msg1 for %s — resetting session", peer_hex)
            sess = NoiseSession.create_responder(_identity.noise_keypair)
            _sessions[peer_hex] = sess

        # Step 1: read the incoming handshake message (may advance internal state)
        if sess.read_handshake_message(pkt.payload) is None:
            log.error("Handshake read failed for %s", peer_hex)
            del _sessions[peer_hex]
            return

        # Step 2: if reading this message established the session (msg3 for responder),
        # we're done — no reply needed.
        if sess.is_established:
            log.info("Noise session ESTABLISHED with %s  remote_pub=%s…",
                     peer_hex, (sess.remote_static_public or b'').hex()[:12])
            return

        # Step 3: session not yet established — write the next handshake message
        # (msg2 for the responder after receiving msg1).
        outgoing = sess.write_handshake_message()
        if outgoing is None:
            log.error("Handshake write failed for %s", peer_hex)
            del _sessions[peer_hex]
            return

        resp_pkt = BitchatPacket(
            type=MessageType.NOISE_HANDSHAKE,
            sender_id=_identity.peer_id,
            recipient_id=pkt.sender_id,
            payload=outgoing,
            ttl=MESSAGE_TTL_HOPS,
            timestamp=int(time.time() * 1000),
        )
        _send_packet(resp_pkt)
        log.info("Noise msg2 sent to %s  %d bytes", peer_hex, len(outgoing))

        if sess.is_established:
            log.info("Noise session ESTABLISHED with %s  remote_pub=%s…",
                     peer_hex, (sess.remote_static_public or b'').hex()[:12])

    elif pkt.type == MessageType.NOISE_ENCRYPTED:
        sess = _sessions.get(peer_hex)
        if sess is None or not sess.is_established:
            log.warning("NOISE_ENCRYPTED from peer with no session: %s", peer_hex)
            return
        plaintext = sess.decrypt(pkt.payload)
        if plaintext is None:
            log.warning("Decrypt failed from %s", peer_hex)
            return
        log.info("DECRYPTED from %s: %s", peer_hex, plaintext[:200])

        # Send "Pong" back as an encrypted private message
        pong_wire = sess.encrypt(_encode_message_payload("Pong"))
        if pong_wire is None:
            log.warning("Could not encrypt Pong")
            return
        _send_packet(BitchatPacket(
            type=MessageType.NOISE_ENCRYPTED,
            sender_id=_identity.peer_id,
            recipient_id=pkt.sender_id,
            payload=pong_wire,
            ttl=MESSAGE_TTL_HOPS,
            timestamp=int(time.time() * 1000),
        ))
        log.info("Pong sent to %s", peer_hex)

    else:
        log.info("PKT type=0x%02x  ttl=%d  from=%s  %d bytes payload",
                 pkt.type, pkt.ttl, peer_hex, len(pkt.payload))


def _on_ble_write(data: bytes, device_path: str | None = None) -> None:
    log.debug("WRITE  %d raw bytes  device=%s", len(data), device_path)
    pkt = decode(data)
    if pkt is None:
        log.warning("Could not decode incoming packet (%d bytes): %s",
                    len(data), data.hex()[:32])
        return
    _handle_packet(pkt)
    # Trigger a reverse BleakClient connection so we receive this phone's notifications
    if device_path and device_path not in _peer_clients and device_path not in _connecting:
        asyncio.get_event_loop().create_task(_reverse_connect(device_path))


async def _reverse_connect(device_path: str) -> None:
    """Connect to a phone's own GATT peripheral so we receive its notifications."""
    _connecting.add(device_path)
    try:
        client = BleakClient(
            device_path,
            disconnected_callback=lambda c: _peer_clients.pop(device_path, None),
        )
        await client.connect()
        log.info("REVERSE connected to %s", device_path)

        def _notify(sender: int, data: bytearray) -> None:
            _on_ble_write(bytes(data), None)

        await client.start_notify(CHARACTERISTIC_UUID.lower(), _notify)
        _peer_clients[device_path] = client
        log.info("REVERSE subscribed to notifications from %s", device_path)
    except Exception as e:
        log.warning("REVERSE connect to %s failed: %s", device_path, e)
    finally:
        _connecting.discard(device_path)


async def run():
    global _gatt, _identity

    _identity = load_or_create(IDENTITY_PATH)
    log.info("peer_id=%s  nick=%s", _identity.peer_id.hex(), NICKNAME)

    _gatt = GattServer()
    await _gatt.start(SERVICE_UUID, CHARACTERISTIC_UUID, _on_ble_write)
    log.info("GATT server registered")

    adv_bus = await start_le_advertisement(SERVICE_UUID, NICKNAME)
    log.info("Advertising as '%s'  service=%s", NICKNAME, SERVICE_UUID)
    log.info("Running for %d s — open BitChat on your phone.", RUN_SECONDS)

    try:
        await asyncio.sleep(RUN_SECONDS)
    finally:
        for client in list(_peer_clients.values()):
            try:
                await client.disconnect()
            except Exception:
                pass
        try:
            await stop_le_advertisement(adv_bus)
        except Exception:
            pass
        await _gatt.stop()
        log.info("Done.")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("Interrupted.")
