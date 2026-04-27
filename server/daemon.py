#!/usr/bin/env python3
"""
BitChatPi daemon — Pi acts as a full BitChat mesh node (peripheral + central).

IPC API (Unix socket, newline-delimited JSON):
  Send commands:  {"cmd":"send","to":"<peer_id_hex>","content":"..."}
                  {"cmd":"broadcast","content":"..."}
                  {"cmd":"send_file","to":"<peer_id_hex>","path":"..."}
                  {"cmd":"peers"}
  Receive events: {"event":"message","from":"...","nick":"...","content":"...","private":true}
                  {"event":"peer","action":"seen"|"lost","peer_id":"...","nick":"..."}
                  {"event":"receipt","type":"delivery"|"read","ref":"...","from":"..."}
                  {"event":"file","from":"...","nick":"...","path":"...","mime":"...","name":"..."}

Usage:
    sudo .venv/bin/python3 server/daemon.py [--nick NAME]
"""
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bitchatd.protocol.constants import (
    SERVICE_UUID, CHARACTERISTIC_UUID, MessageType,
)
from bitchatd.mesh.relay_engine import RelayEngine
from bitchatd.mesh.fragment_manager import FragmentManager, _decode_fragment_payload
from bitchatd.mesh.session_manager import SessionManager
from bitchatd.protocol.packet import BitchatPacket
from bitchatd.protocol.codec import encode, decode
from bitchatd.crypto.identity import load_or_create
from bitchatd.ble.gatt_server import GattServer
from bitchatd.ble.scanner import BleScanner
from bitchatd.ble.advertise import start_le_advertisement, stop_le_advertisement
from bitchatd.api import IpcServer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("dissononce").setLevel(logging.WARNING)
log = logging.getLogger("smoke")

IDENTITY_PATH = Path.home() / ".config" / "bitchatd" / "identity.json"
IPC_SOCK_PATH = str(Path.home() / ".config" / "bitchatd" / "api.sock")

_gatt:        GattServer    | None = None
_scanner:     BleScanner    | None = None
_ipc:         IpcServer     | None = None
_relay:       RelayEngine   | None = None
_fragments:   FragmentManager | None = None
_session_mgr: SessionManager  | None = None
_identity     = None  # set in run()


# ── IPC fan-out ────────────────────────────────────────────────────────────────

def _publish(event: dict) -> None:
    if _ipc is not None:
        asyncio.ensure_future(_ipc.publish(event))


# ── BLE send helpers ───────────────────────────────────────────────────────────

def _send_packet(pkt: BitchatPacket) -> None:
    raw = encode(pkt, pad=True)
    if raw is None:
        log.warning("Failed to encode packet")
        return
    if _gatt is None:
        log.warning("GATT server not ready")
        return
    _gatt.send(raw)
    if _scanner is not None and _scanner.connected_count > 0:
        asyncio.ensure_future(_scanner.send(raw))
    log.info("SENT type=0x%02x  %d bytes", pkt.type, len(raw))


def _fragment_and_send(pkt: BitchatPacket) -> None:
    """Fragment pkt if it exceeds the BLE MTU, then send each fragment."""
    if _fragments:
        for frag in _fragments.create_fragments(pkt):
            _send_packet(frag)
    else:
        _send_packet(pkt)


# ── Relay callback ─────────────────────────────────────────────────────────────

async def _relay_broadcast(pkt: BitchatPacket, from_peer_id: str) -> None:
    """RelayEngine callback: re-broadcast a relayed packet to all connected peers."""
    raw = encode(pkt, pad=True)
    if not raw:
        return
    if _gatt:
        _gatt.send(raw)
    if _scanner and _scanner.connected_count > 0:
        await _scanner.send(raw)
    log.debug("RELAY type=0x%02x  ttl=%d  from=%s  %d bytes",
              pkt.type, pkt.ttl, from_peer_id[:8], len(raw))


def _schedule_relay(pkt: BitchatPacket, from_peer_hex: str) -> None:
    if _relay is not None:
        asyncio.ensure_future(_relay.handle_relay(pkt, from_peer_hex))


# ── Packet routing ─────────────────────────────────────────────────────────────

def _dispatch(pkt: BitchatPacket) -> None:
    """Route an incoming packet: process locally if for us, relay if appropriate."""
    if _identity and pkt.sender_id == _identity.peer_id:
        return

    peer_hex = pkt.sender_id.hex()

    if pkt.type == MessageType.FRAGMENT:
        if _fragments is not None:
            decoded = _decode_fragment_payload(pkt.payload)
            if decoded:
                _fid, _idx, _tot, _otype, _data = decoded
                log.debug("FRAGMENT from %s  [%d/%d]  orig_type=0x%02x  %d bytes",
                          peer_hex, _idx + 1, _tot, _otype, len(_data))
            reassembled = _fragments.handle_fragment(pkt)
            if reassembled is not None:
                log.info("FRAGMENT reassembled from %s  original_type=0x%02x  %d bytes",
                         peer_hex, reassembled.type, len(reassembled.payload))
                _dispatch(reassembled)
        # Don't relay fragments addressed to us — relaying our own DM fragments
        # causes RF contention with the sender's next write, dropping packets.
        addressed_to_us = (
            _identity is not None and
            pkt.recipient_id is not None and
            pkt.recipient_id == _identity.peer_id
        )
        if not addressed_to_us:
            _schedule_relay(pkt, peer_hex)
        return

    is_for_me = pkt.is_broadcast or (
        _identity is not None and
        pkt.recipient_id is not None and
        pkt.recipient_id == _identity.peer_id
    )
    # Noise packets must be processed even when recipient_id doesn't match exactly
    # (phone may address to a stale peer_id or omit recipient entirely).
    # For NOISE_HANDSHAKE: a bare msg1 (32-byte ephemeral key) is safe to accept from anyone.
    # For NOISE_ENCRYPTED: only process if we already have session/peer state; the decrypt
    # is the authoritative gate against acting on packets not meant for us.
    if not is_for_me:
        if pkt.type == MessageType.NOISE_HANDSHAKE and len(pkt.payload) == 32:
            is_for_me = True
            log.debug("NOISE_HANDSHAKE msg1 from %s — accepting regardless of recipient_id",
                      peer_hex)
        elif pkt.type in (MessageType.NOISE_HANDSHAKE, MessageType.NOISE_ENCRYPTED):
            if _session_mgr is not None and _session_mgr.has_session(peer_hex):
                log.debug("Noise pkt recipient_id mismatch from %s — processing via session fallback",
                          peer_hex)
                is_for_me = True

    if is_for_me and _session_mgr is not None:
        _session_mgr.handle_packet(pkt)

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

    # When the first fragment of a new multi-fragment set arrives via the peripheral
    # write path (device_path is set), release all scanner central connections.
    # Android phones use random resolvable addresses that rotate, so the scanner's
    # central connection to the phone likely uses a different MAC than the peripheral
    # connection.  Two simultaneous BLE connections (central + peripheral) on the same
    # radio chip cause connection-event scheduling conflicts that drop the peripheral
    # write after ~25 fragments.  Releasing the central connection eliminates the
    # conflict; the scanner reconnects automatically after the transfer completes.
    if device_path is not None and pkt.type == MessageType.FRAGMENT and _scanner and _fragments:
        frag_decoded = _decode_fragment_payload(pkt.payload)
        if frag_decoded is not None:
            frag_id, _, total, _, _ = frag_decoded
            if _fragments.is_new_set(frag_id):
                asyncio.ensure_future(_scanner.disconnect_all_central())

    # Defer dispatch to the next event-loop tick so WriteValue returns first.
    # BlueZ only sends ATT_WRITE_RSP (write ack) after the method reply; if we
    # emit a PropertiesChanged notification before that, some phones receive the
    # GATT notification before their write is acknowledged and silently drop it.
    try:
        asyncio.get_running_loop().call_soon(lambda: _dispatch(pkt))
    except RuntimeError:
        _dispatch(pkt)


# ── IPC command handler ────────────────────────────────────────────────────────

async def _ipc_command_handler(cmd: dict) -> dict | None:
    if _session_mgr is None:
        return {"ok": False, "error": "daemon not ready"}
    return await _session_mgr.handle_command(cmd)


# ── Adapter readiness ──────────────────────────────────────────────────────────

async def _run_bluetoothctl(*args: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        "bluetoothctl", *args,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.communicate()


async def _ensure_adapter_up() -> None:
    """
    Soft-reset the BLE adapter (power off → on) then wait for Powered: yes.

    The power cycle clears any stale GATT application or advertisement
    registrations left by a previous process crash — far faster than a full
    `systemctl restart bluetooth` and avoids the 2-5 minute rediscovery delay.
    """
    log.info("Soft-resetting BLE adapter to clear stale registrations…")
    await _run_bluetoothctl("power", "off")
    await asyncio.sleep(1)
    await _run_bluetoothctl("power", "on")

    for attempt in range(15):
        proc = await asyncio.create_subprocess_exec(
            "bluetoothctl", "show",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        if b"Powered: yes" in out:
            log.info("BLE adapter ready after %d s", attempt + 1)
            return
        await asyncio.sleep(1)
    log.error("BLE adapter not ready after 15 s — advertising may fail")


# ── Main ───────────────────────────────────────────────────────────────────────

async def run(nickname: str) -> None:
    global _gatt, _scanner, _ipc, _relay, _fragments, _session_mgr, _identity

    await _ensure_adapter_up()

    _identity = load_or_create(IDENTITY_PATH)
    log.info("peer_id=%s  nick=%s", _identity.peer_id.hex(), nickname)

    _relay = RelayEngine(_identity.peer_id)
    _relay.broadcast_packet = _relay_broadcast
    _relay.get_network_size = lambda: max(1, len(_session_mgr.peers) if _session_mgr else 1)

    _fragments = FragmentManager()
    _fragments.start()

    _session_mgr = SessionManager(
        identity=_identity,
        nickname=nickname,
        send_packet=_send_packet,
        fragment_and_send=_fragment_and_send,
        publish=_publish,
    )

    def _on_notify_subscribed() -> None:
        async def _do_announce() -> None:
            if _session_mgr:
                _session_mgr.clear_announce_cooldown()
                _send_packet(_session_mgr.make_announce())
                log.info("StartNotify triggered — sent ANNOUNCE to new subscriber")
        asyncio.ensure_future(_do_announce())

    def _on_peer_disconnected(address: str) -> None:
        if _gatt:
            _gatt.remove_device(address)

    _gatt = GattServer()
    await _gatt.start(SERVICE_UUID, CHARACTERISTIC_UUID, _on_ble_write,
                      on_start_notify=_on_notify_subscribed)
    log.info("GATT server registered")

    _scanner = BleScanner(SERVICE_UUID, CHARACTERISTIC_UUID, _on_ble_write,
                          on_peer_disconnected=_on_peer_disconnected,
                          get_peripheral_addrs=lambda: _gatt.connected_addresses if _gatt else set())
    await _scanner.start()
    log.info("BLE scanner started (central role)")

    _ipc = IpcServer(IPC_SOCK_PATH)
    _ipc.set_command_handler(_ipc_command_handler)
    await _ipc.start()

    adv_bus = await start_le_advertisement(SERVICE_UUID, nickname)
    log.info("Advertising as '%s'  service=%s", nickname, SERVICE_UUID)
    log.info("Running — press Ctrl-C to stop.")

    async def _periodic_announce() -> None:
        while True:
            await asyncio.sleep(60)
            if _session_mgr is not None:
                _send_packet(_session_mgr.make_announce())
                log.debug("Periodic ANNOUNCE broadcast")

    asyncio.ensure_future(_periodic_announce())

    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        try:
            await stop_le_advertisement(adv_bus)
        except Exception:
            pass
        if _scanner:
            await _scanner.stop()
        await _gatt.stop()
        await _ipc.stop()
        if _fragments:
            _fragments.stop()
        log.info("Done.")


if __name__ == "__main__":
    import argparse
    import signal

    parser = argparse.ArgumentParser(description="BitChatPi mesh daemon")
    parser.add_argument(
        "--nick", default="BitChatPi", metavar="NAME",
        help="Nickname to broadcast on the mesh (default: %(default)s)",
    )
    args = parser.parse_args()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    main_task = loop.create_task(run(args.nick))

    def _shutdown(signum, frame):
        loop.call_soon_threadsafe(main_task.cancel)

    signal.signal(signal.SIGTERM, _shutdown)

    try:
        loop.run_until_complete(main_task)
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Interrupted.")
    finally:
        loop.close()
