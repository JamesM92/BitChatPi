# BitChatPi — Architecture Design
Date: 2026-04-25
Protocol verified against upstream commit: 5b0a7d0 (2026-03-26)

---

## Goal
Run a BitChat-compatible Bluetooth mesh node on a Raspberry Pi as a background daemon.
Local software connects to the daemon via a simple IPC API to send/receive messages.
Nostr/internet features are present but disabled by default, with a config flag to enable.
No telemetry whatsoever.

The Android repo (https://github.com/permissionlesstech/bitchat-android) is the absolute
authority on the protocol. See `upstream/UPSTREAM.md` for the tracking strategy.

---

## Upstream tracking

Every Pi module that implements part of the wire protocol is derived from a specific Android
source file. The mapping is maintained in `upstream/UPSTREAM.md`.

Two tools enforce compliance:

| Tool | Purpose |
|---|---|
| `upstream/scripts/fetch-upstream.sh` | Pull latest Android source snapshots |
| `upstream/scripts/check-compat.py` | Assert Pi constants match Android — exits 1 on failure |

Run both before every release. If the Android repo changes a tracked file, the workflow is:
1. `fetch-upstream.sh` updates the snapshot
2. `check-compat.py` identifies what broke
3. Fix the Pi file
4. Update the `Last-verified commit` column in UPSTREAM.md

---

## Protocol contract (verified from raw source)

### BLE GATT UUIDs — from AppConstants.kt, commit 5b0a7d0
```
SERVICE_UUID        = F47B5E2D-4A9E-4C5A-9B3F-8E1D2C3A4B5C
CHARACTERISTIC_UUID = A1B2C3D4-E5F6-4A5B-8C9D-0E1F2A3B4C5D
DESCRIPTOR_UUID     = 00002902-0000-1000-8000-00805f9b34fb  (standard CCCD)
```

### Outer packet header — from BinaryProtocol.kt
```
Version 1: 13 bytes
  [0]    version       uint8
  [1]    type          uint8  (see packet types below)
  [2]    ttl           uint8  (starts at 7, decremented each hop)
  [3:11] timestamp     uint64 big-endian (ms since epoch)
  [11]   flags         uint8
  [12:14] payload_len  uint16 big-endian

Version 2: 15 bytes
  same as v1 but payload_len is uint32 big-endian (4 bytes)

After header (in order):
  sender_id     8 bytes fixed
  recipient_id  8 bytes (if flags & 0x01)
  route         1-byte hop count + N×8 bytes (v2 only, if flags & 0x08)
  payload       variable (may be zlib-compressed; see IS_COMPRESSED flag)
  signature     64 bytes (if flags & 0x02)

BROADCAST recipient = 0xFFFFFFFFFFFFFFFF
```

### Flags byte
```
HAS_RECIPIENT = 0x01
HAS_SIGNATURE = 0x02
IS_COMPRESSED = 0x04
HAS_ROUTE     = 0x08  (v2+ only)
```

### Packet types
```
ANNOUNCE        = 0x01
MESSAGE         = 0x02
LEAVE           = 0x03
NOISE_HANDSHAKE = 0x10
NOISE_ENCRYPTED = 0x11
FRAGMENT        = 0x20
REQUEST_SYNC    = 0x21
FILE_TRANSFER   = 0x22
```

### Fragment header — from FragmentPayload.kt, commit 9795e2c
```
13-byte header inside each FRAGMENT packet's payload:
  [0:8]   fragment_id    8 random bytes (identifies the fragmented message)
  [8:10]  index          uint16 big-endian (0-based)
  [10:12] total          uint16 big-endian
  [12]    original_type  uint8 (the packet type being fragmented)
  [13:]   fragment data

Safety limits (AppConstants.kt):
  FRAGMENT_SIZE_THRESHOLD   = 512 bytes (fragment if > this after encoding)
  MAX_FRAGMENT_SIZE         = 469 bytes max data per chunk
  MAX_FRAGMENTS_PER_ID      = 256
  MAX_FRAGMENT_TOTAL_BYTES  = 1,048,576 bytes per fragment set
  MAX_ACTIVE_FRAGMENT_SETS  = 64
  MAX_GLOBAL_FRAGMENT_BYTES = 4,194,304 bytes total buffered

NOTE: Reassembled packet's TTL is set to 0 to suppress re-relay.
```

### Relay probabilities — from PacketRelayManager.kt, commit 66012e9
```
TTL on new message = 7
TTL >= 4           → always relay (bypass probability check)
peer count ≤ 3     → always relay
peer count ≤ 10    → 100%   (always relay — NOT 85% as often misquoted)
peer count ≤ 30    → 85%
peer count ≤ 50    → 70%
peer count ≤ 100   → 55%
peer count > 100   → 40%
```

### BitchatMessage inner payload — from BitchatMessage.kt, commit 633a506
The payload of a `MESSAGE` packet contains a serialised `BitchatMessage`:
```
[0]     flags uint8:
          0x01 = isRelay
          0x02 = isPrivate
          0x04 = hasOriginalSender
          0x08 = hasRecipientNickname
          0x10 = hasSenderPeerID
          0x20 = hasMentions
          0x40 = hasChannel
          0x80 = isEncrypted
[1:9]   timestamp int64 ms since epoch (big-endian)
[9]     id_len uint8  +  id bytes (UTF-8, max 255)
[+1]    sender_len uint8  +  sender nickname bytes (UTF-8, max 255)
[+2]    content_len uint16  +  content or encryptedContent (max 65535)
optional (flags-gated, each as uint8 len + UTF-8 bytes):
  originalSender, recipientNickname, senderPeerID
  mentions: uint8 count + (uint8 len + UTF-8) × count
  channel: uint8 len + UTF-8
```

---

## Pi Client-Server Architecture

```
┌─────────────────────────────────────────────────────────┐
│                   Raspberry Pi                          │
│                                                         │
│  ┌──────────────────────────────────────────────────┐   │
│  │            bitchatd  (daemon / systemd)          │   │
│  │                                                  │   │
│  │  ┌──────────────┐    ┌──────────────────────┐   │   │
│  │  │  BLE Layer   │    │   Protocol Engine    │   │   │
│  │  │  (BlueZ/     │◄──►│  - Packet codec     │   │   │
│  │  │   bleak)     │    │  - Noise crypto      │   │   │
│  │  │  GATT server │    │  - Fragment mgr      │   │   │
│  │  │  + scanner   │    │  - Relay engine      │   │   │
│  │  └──────────────┘    │  - Peer manager      │   │   │
│  │                      │  - Store-forward     │   │   │
│  │  ┌──────────────┐    └──────────────────────┘   │   │
│  │  │  Nostr       │              │                 │   │
│  │  │  Bridge      │              │                 │   │
│  │  │  (disabled   │    ┌─────────▼──────────┐     │   │
│  │  │   by default)│    │    IPC API Server  │     │   │
│  │  └──────────────┘    │  Unix socket /     │     │   │
│  │                      │  localhost TCP      │     │   │
│  └──────────────────────┴────────────────────┘     │   │
│                               │                     │   │
│          ┌────────────────────┼──────────────┐      │   │
│          ▼                    ▼              ▼      │   │
│   ┌─────────────┐   ┌──────────────┐  ┌──────────┐ │   │
│   │  CLI client │   │  Web UI /    │  │  Custom  │ │   │
│   │  (terminal) │   │  REST bridge │  │  app     │ │   │
│   └─────────────┘   └──────────────┘  └──────────┘ │   │
└─────────────────────────────────────────────────────────┘
```

---

## Component Breakdown

### 1. BLE Transport (`ble/`)
**Language**: Python with `bleak` ≥ 0.22 + `dbus-fast`

- GATT server: advertise Service UUID, accept characteristic writes (inbound packets)
- Scanner: scan for peers advertising Service UUID, connect, subscribe to notifications
- Both roles run concurrently on the same `hci0` adapter

**HIGHEST RISK ITEM — BLE dual role on Linux:**
BlueZ GATT server + active scanning simultaneously is the most likely blocker in
Phase 1. Known issues:
- `BleakServer` on Linux requires careful D-Bus ordering
- Some BlueZ versions have race conditions starting advertising while scanning
- Mitigation: start GATT server first, delay scanner start by 1–2 seconds
- Fallback: drive BlueZ directly via `dbus-fast` if `bleak` proves unreliable
- Test this in isolation before building anything else

MAC address rotation: identify peers by the peerID bytes in the scan response
(first 8 bytes of peerID, set as service data), not by MAC address.

MTU: request 517 as Android does; negotiate down if needed. Fragment threshold
remains 512; max fragment payload remains 469.

### 2. Packet Codec (`protocol/`)
- Pure-Python implementation of BinaryProtocol
- `encode(packet) -> bytes` / `decode(bytes) -> BitchatPacket`
- Compression: zlib when payload > 100 bytes AND compresses beneficially
- PKCS#7-style padding to 256/512/1024/2048 block sizes
- All 8 message types
- **Every constant references `upstream/android/AppConstants.kt` in a comment**
- `check-compat.py` validates this file at CI time

### 3. Noise Encryption (`crypto/`)
- Pattern: Noise_XX_25519_ChaChaPoly_SHA256
- Library: `dissononce` (actively maintained, correct XX implementation)
- Per-peer session state machine; one session per connected peerID
- Rekey after 1 hour or 10,000 messages (AppConstants.Noise)
- Ed25519 signing: `cryptography` (PyCA) for ANNOUNCE packet signatures
- Identity persistence: `~/.config/bitchatd/identity.json`
  - Curve25519 static keypair (for Noise)
  - Ed25519 keypair (for announcements)
  - peerID = first 8 bytes of SHA-256(static_public_key), hex-encoded

### 4. Mesh Engine (`mesh/`)
- **PeerManager**: active peer registry keyed by peerID; evict after 180s inactivity
- **RelayEngine**: TTL decrement, probabilistic relay (exact probabilities from
  PacketRelayManager.kt — see protocol contract above), LRU dedup cache (10,000 entries)
- **FragmentManager**: split outbound, reassemble inbound with all safety limits
  from AppConstants.Fragmentation; set TTL=0 on reassembled packet
- **StoreForward**: cache up to 100 messages (1,000 for favourites) for 12 hours;
  replay to peer on reconnect

### 5. Message Handler (`messages/`)
- ANNOUNCE: sign with Ed25519, broadcast on connect; verify incoming signatures
- MESSAGE broadcast: wrap in BitchatMessage, encode payload, send as BROADCAST
- MESSAGE private: encrypt with peer's Noise session, set recipientID
- LEAVE: broadcast on graceful shutdown; evict peer on receipt
- DeliveryAck: send on receipt of private message; surface to IPC clients

### 6. Geohash Support (`geo/`)
- Library: `pygeohash` — pure local computation, no internet or GPS required
- Pi geohash source (in priority order):
  1. `geo.geohash` in config (static, recommended for fixed installations)
  2. gpsd (if `geo.gpsd = true` and gpsd is running)
  3. Disabled (if neither set — Pi acts as geohash-agnostic relay)
- Geohash channel = broadcast MESSAGE with `channel` field set to geohash string
  (matches Android behaviour via BitchatMessage.channel)
- Optional Nostr bridge for cross-mesh geohash delivery (disabled by default)

### 7. IPC API Server (`api/`)
The interface between the daemon and local client software.

**Transport**: newline-delimited JSON over Unix domain socket
  Default: `/run/bitchatd/api.sock`
  Optional TCP: `localhost:7331` (set `api.tcp_port` in config)

**Multi-client**: daemon maintains a set of connected IPC clients; all events
are fanned out to every connected client. `subscribe` narrows which events
a client receives.

**Client → Daemon messages:**
```json
{"type": "send", "channel": "global", "text": "hello mesh"}
{"type": "send", "channel": "geo:gcpvj", "text": "regional message"}
{"type": "send", "to": "<peerID>", "text": "private message"}
{"type": "subscribe", "channels": ["global", "geo:gcpvj"]}
{"type": "status"}
```

**Daemon → Client events:**
```json
{"type": "message", "from": "<peerID>", "nick": "Alice", "channel": "global",
 "text": "hello", "timestamp": 1745600000000, "private": false}

{"type": "delivery_ack", "message_id": "<uuid>", "from": "<peerID>"}

{"type": "peer_joined", "peerID": "abc123", "nick": "Bob"}
{"type": "peer_left",   "peerID": "abc123"}

{"type": "status", "peers": 3, "peerID": "...", "nick": "...",
 "geohash": "gcpvj", "nostr_enabled": false}

{"type": "error", "code": "peer_not_found", "detail": "peerID abc123 not connected"}
```

**Error responses**: daemon always sends `{"type": "error", ...}` in response to
invalid requests rather than silently dropping them.

### 8. Configuration
File: `/etc/bitchatd/config.toml` (system) or `~/.config/bitchatd/config.toml` (user)

```toml
[identity]
nickname = "PiNode"       # max 15 chars (AppConstants.UI.MAX_NICKNAME_LENGTH)
# keypair auto-generated on first run; stored alongside this file as identity.json

[bluetooth]
adapter = "hci0"

[geo]
enabled = true
geohash = "gcpvj"         # static geohash — comment out to disable geo channels
# gpsd = true             # uncomment to derive geohash from gpsd instead

[nostr]
# Internet bridge — DISABLED by default. Geohash channels work over BLE without this.
enabled = false
relays = [
  "wss://relay.damus.io",
  "wss://relay.primal.net",
  "wss://offchain.pub",
  "wss://nostr21.com",
]
tor_proxy = ""            # e.g. "socks5://127.0.0.1:9050"

[telemetry]
# Zero telemetry. This key exists only to be explicit — nothing sends telemetry.
enabled = false

[api]
socket = "/run/bitchatd/api.sock"
# tcp_port = 7331
```

---

## Technology Stack

| Component       | Library / Tool          | Notes |
|-----------------|-------------------------|-------|
| BLE             | `bleak` ≥ 0.22 + `dbus-fast` | BlueZ D-Bus on Linux |
| Noise protocol  | `dissononce`            | XX pattern, Curve25519 |
| Ed25519 signing | `cryptography` (PyCA)   | Key generation + signing |
| Compression     | `zlib` (stdlib)         | Matches Android CompressionUtil |
| Geohash         | `pygeohash`             | Local, no internet |
| Config          | `tomllib` (stdlib 3.11+)| TOML parsing |
| IPC             | `asyncio` Unix socket   | stdlib, no extra deps |
| Nostr (optional)| `websockets`            | Only when nostr.enabled = true |
| Daemon          | systemd unit file       | Auto-start on boot |

**Runtime**: Python 3.11+ (available on Pi OS Bookworm)

---

## File/Module Layout

```
BitChatPi/
├── upstream/                        ← Android source authority
│   ├── UPSTREAM.md                  # Tracking table + protocol contract
│   ├── android/                     # Raw snapshots of tracked Android files
│   │   ├── AppConstants.kt
│   │   ├── BinaryProtocol.kt
│   │   ├── FragmentPayload.kt
│   │   ├── FragmentManager.kt
│   │   ├── PacketRelayManager.kt
│   │   ├── BluetoothGattServerManager.kt
│   │   ├── BluetoothGattClientManager.kt
│   │   └── BitchatMessage.kt
│   └── scripts/
│       ├── fetch-upstream.sh        # Pull latest Android files from GitHub
│       └── check-compat.py          # Assert Pi constants match Android
│
├── bitchatd/
│   ├── __main__.py
│   ├── daemon.py
│   ├── config.py
│   ├── ble/
│   │   ├── gatt_server.py           # Derived from BluetoothGattServerManager.kt
│   │   ├── scanner.py               # Derived from BluetoothGattClientManager.kt
│   │   └── transport.py
│   ├── protocol/
│   │   ├── packet.py                # Derived from BinaryProtocol.kt
│   │   ├── codec.py                 # Derived from BinaryProtocol.kt
│   │   └── constants.py             # Derived from AppConstants.kt — checked by check-compat.py
│   ├── crypto/
│   │   ├── identity.py
│   │   ├── noise_session.py
│   │   └── signing.py
│   ├── mesh/
│   │   ├── peer_manager.py
│   │   ├── relay_engine.py          # Derived from PacketRelayManager.kt
│   │   ├── fragment_manager.py      # Derived from FragmentManager.kt + FragmentPayload.kt
│   │   └── store_forward.py
│   ├── messages/
│   │   ├── handler.py
│   │   ├── message.py               # Derived from BitchatMessage.kt
│   │   ├── broadcast.py
│   │   └── private.py
│   ├── geo/
│   │   └── geohash.py
│   ├── nostr/
│   │   ├── bridge.py
│   │   └── relay_dir.py
│   └── api/
│       ├── server.py
│       └── protocol.py
│
├── systemd/
│   └── bitchatd.service
├── config/
│   └── config.example.toml
├── tests/
│   ├── test_codec.py                # Wire-level encode/decode round-trips
│   ├── test_fragment.py             # Fragment split + reassembly with safety limits
│   ├── test_relay.py                # TTL, relay probabilities
│   ├── test_message.py              # BitchatMessage serialisation
│   └── test_compat.py               # Runs check-compat.py as a test
└── References/                      # Research archive (see References/README.md)
```

---

## Implementation Phases

### Phase 0 — Protocol contract (prerequisite for everything)
1. Run `fetch-upstream.sh` to populate `upstream/android/`
2. Write `bitchatd/protocol/constants.py` from `AppConstants.kt`
3. Run `check-compat.py` — must pass before any other code is written
4. Write `test_compat.py` so CI enforces this permanently

### Phase 1 — Core mesh (no encryption)
1. Protocol codec: encode/decode all 8 packet types
2. BitchatMessage inner payload serialisation
3. Fragment manager (with all safety limits)
4. BLE GATT server + scanner — **start with a standalone BLE smoke test before
   integrating; this is the highest-risk item**
5. Relay engine (correct probabilities from upstream)
6. Peer manager
7. IPC API (Unix socket, basic send/receive, fan-out to multiple clients)
8. Milestone: Pi nodes exchange plaintext broadcast messages with each other

### Phase 2 — Wire compatibility with Android/iOS
1. Noise XX crypto (dissononce)
2. Identity keypair + persistence
3. Ed25519 ANNOUNCE signing + verification
4. Private message routing (encrypted)
5. DeliveryAck generation + IPC surfacing
6. Milestone: Pi node exchanges encrypted messages with real Android BitChat app

### Phase 3 — Geohash channels
1. Static geohash config
2. Optional gpsd integration
3. Geohash-tagged broadcast (BitchatMessage.channel field)
4. IPC subscription filtering by channel

### Phase 4 — Optional Nostr bridge
1. Nostr WebSocket client (websockets library)
2. Port RelayDirectory.kt geohash→relay mapping
3. Publish/subscribe geohash channels to Nostr relays
4. Disabled unless `nostr.enabled = true`
5. Optional Tor SOCKS5 proxy

### Phase 5 — Polish
1. systemd unit + install script
2. `bitchat-cli` simple CLI client
3. Comprehensive test suite
4. Documentation
