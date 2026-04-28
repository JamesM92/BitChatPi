# Changelog

## [1.0.0] — 2026-04-28

Initial release of BitChatPi — a Raspberry Pi BLE mesh node fully compatible with the [BitChat](https://github.com/permadao/BitChat) Android app.

---

### Server daemon (`server/`)

**BLE mesh node**
- Dual-role operation: GATT peripheral (advertising + accepting connections from phones) and BLE central (scanning for and connecting to other BitChat peers) running simultaneously on the same adapter
- BlueZ D-Bus GATT server exposing the BitChat service UUID with correct characteristic properties (write-without-response + notify)
- LE advertisement via `LEAdvertisingManager1` with `peripheral` type for connectable `ADV_IND` packets, tx-power included, 150 ms interval for fast peer discovery
- Automatic Bluetooth adapter power cycle on startup to clear stale BlueZ advertiser state
- Advertisement pause/resume around fragment reception to reduce radio contention

**Encryption and identity**
- Noise XX end-to-end encryption for all private messages and file transfers, using [dissononce](https://github.com/tgalal/dissononce)
- Ed25519 identity keypair persisted to `~/.config/bitchatd/identity.json`; generated on first run, stable across restarts
- Automatic session self-healing: after 3 consecutive decrypt failures the daemon sends `LEAVE` + `ANNOUNCE` to force the peer to initiate a fresh Noise handshake, recovering from cipher-state desynchronisation without manual intervention
- Outgoing messages queued when no session exists; delivered automatically once the handshake completes

**Fragment reassembly**
- Full BitChat fragment protocol (type `0x20`): 13-byte header, up to 65 535 fragments per set
- Rescue cache: partial sets that reach ≥ 75 % completion are saved for up to 60 minutes; a retransmission inherits cached fragments so only missing pieces need to be resent
- Per-attempt tracking: attempt counter persists across completions so late-arriving stale fragments never reset the counter and generate spurious notifications
- Stale-set purge: when reassembly succeeds any other in-progress sets for the same image are discarded immediately, preventing delayed `fragment_partial` fires

**Auto-reply for incomplete transfers**
- When a fragment set expires without completing, the daemon sends a DM to the sender: `[auto] Transfer incomplete (attempt #N, X/Y fragments, ~ZKB, missing=[…]). Please try again.`
- Missing indices expressed as compact ranges (e.g. `[46-47, 100]`)
- Rate-limited to one reply per sender per minute to avoid flooding on rapid retransmissions
- Queued auto-replies are cancelled automatically if reassembly later succeeds

**Relay engine**
- Probabilistic forwarding with TTL decay compatible with the BitChat mesh routing model
- Duplicate-suppression cache to prevent loops

**IPC API**
- Unix domain socket at `/root/.config/bitchatd/api.sock` (world-writable after install)
- Newline-delimited JSON, full-duplex, multiple simultaneous clients supported
- Commands: `ping`, `peers`, `set_nick`, `send`, `broadcast`, `send_file`
- Events: `hello`, `message`, `peer`, `receipt`, `file`, `fragment_partial`, `fragment_set_started`, `fragment_completed`
- See [IPC_API.md](IPC_API.md) for the complete reference

**systemd service**
- Installed as `bitchatd.service`; enabled on boot, restarts automatically on failure
- Bluetooth adapter restarted as part of `ExecStartPre` to ensure clean BLE state
- IPC socket chmod'd to `0666` after startup so non-root clients can connect without sudo
- Log file: `/root/.config/bitchatd/bitchatd.log`

---

### TUI client (`client/`) — reference implementation

- urwid terminal UI with a peer panel (left), per-peer DM channels, and a global broadcast channel
- File send (`/send <path>`) and receive with inline status rows
- Image preview via Img2ContourAscii: 24-bit colour KD-tree ASCII renderer, bounded to terminal dimensions
- Fragment transfer progress: `[PARTIAL]` rows on timeout, `[RESUMING]` rows when a retry inherits cached fragments, inline completion note when a multi-attempt transfer succeeds
- Peer join/leave notifications, delivery and read receipts

---

### Infrastructure

- `install-server.sh` — installs to `/opt/bitchatpi/`, creates venv, pins dependencies, registers and starts the systemd service
- `install-client.sh` — adds TUI dependencies to the existing venv
- `restart-service.sh` — restarts `bitchatd` and polls for the IPC socket (up to 20 s) before returning
- `start-server.sh` — foreground/dev mode: stops the service, runs daemon interactively, restarts service on exit
- `tests/` — pytest suite covering packet codec, crypto primitives, relay engine, and fragment reassembly
