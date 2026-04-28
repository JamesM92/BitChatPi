# Upstream Tracking — Android BitChat Authority

The Android repository is the **absolute authority** on the protocol.
Any discrepancy between files in this repo and the Android source is a bug here.

Android repo: https://github.com/permissionlesstech/bitchat-android

---

## Tracked files

| Android source file | Last-verified commit | Date | Pi file(s) derived from it |
|---|---|---|---|
| `util/AppConstants.kt` | `5b0a7d0` | 2026-03-26 | `bitchatd/protocol/constants.py` |
| `protocol/BinaryProtocol.kt` | `5b0a7d0` | 2026-03-26 | `bitchatd/protocol/codec.py`, `bitchatd/protocol/packet.py` |
| `model/FragmentPayload.kt` | `9795e2c` | 2025-08-18 | `bitchatd/mesh/fragment_manager.py` |
| `mesh/FragmentManager.kt` | `5b0a7d0` | 2026-03-26 | `bitchatd/mesh/fragment_manager.py` |
| `mesh/PacketRelayManager.kt` | `66012e9` | 2026-01-12 | `bitchatd/mesh/relay_engine.py` |
| `mesh/BluetoothGattServerManager.kt` | `c64ea0a` | 2026-01-15 | `bitchatd/ble/gatt_server.py` |
| `mesh/BluetoothGattClientManager.kt` | `c64ea0a` | 2026-01-15 | `bitchatd/ble/scanner.py` |
| `model/BitchatMessage.kt` | `633a506` | 2025-09-19 | `bitchatd/mesh/session_manager.py` (inline encoder) |

---

## Protocol contract — must-match values

These values are extracted from upstream and verified by `scripts/check-compat.py`.
**Do not change them in Pi code without a confirmed upstream change.**

### BLE GATT UUIDs (AppConstants.kt → Mesh.Gatt)
```
SERVICE_UUID        = F47B5E2D-4A9E-4C5A-9B3F-8E1D2C3A4B5C
CHARACTERISTIC_UUID = A1B2C3D4-E5F6-4A5B-8C9D-0E1F2A3B4C5D
DESCRIPTOR_UUID     = 00002902-0000-1000-8000-00805f9b34fb
```

### Packet header (BinaryProtocol.kt)
```
Version 1 header = 13 bytes  (version:1 + type:1 + ttl:1 + timestamp:8 + flags:1 + payloadLen:2)
Version 2 header = 15 bytes  (same but payloadLen is 4 bytes)
SenderID         = 8 bytes fixed
RecipientID      = 8 bytes (if HAS_RECIPIENT flag)
BROADCAST_ID     = 0xFFFFFFFFFFFFFFFF
Signature        = 64 bytes (if HAS_SIGNATURE flag)
```

### Packet types (BinaryProtocol.kt)
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

### Flags byte (BinaryProtocol.kt)
```
HAS_RECIPIENT = 0x01
HAS_SIGNATURE = 0x02
IS_COMPRESSED = 0x04
HAS_ROUTE     = 0x08  (v2+ only)
```

### Fragmentation (AppConstants.kt → Fragmentation + FragmentPayload.kt)
```
FRAGMENT_SIZE_THRESHOLD   = 512        bytes (fragment if encoded packet > this)
MAX_FRAGMENT_SIZE         = 469        bytes (max data per fragment chunk)
FRAGMENT_TIMEOUT_MS       = 30_000     ms
MAX_FRAGMENTS_PER_ID      = 256
MAX_FRAGMENT_TOTAL_BYTES  = 1_048_576  bytes per fragment set
MAX_ACTIVE_FRAGMENT_SETS  = 64
MAX_GLOBAL_FRAGMENT_BYTES = 4_194_304  bytes (4 × 1MB)

Fragment header layout (FragmentPayload.kt) — 13 bytes:
  [0:8]   fragment_id  (8 random bytes)
  [8:10]  index        (uint16 big-endian, 0-based)
  [10:12] total        (uint16 big-endian)
  [12]    original_type (uint8)
  [13:]   fragment data

Reassembled packet TTL is set to 0 to suppress re-relay.
```

### Relay logic (PacketRelayManager.kt)
```
TTL on new message = 7
TTL >= 4           → always relay (bypass probability)
Network size ≤ 3   → always relay
Network size ≤ 10  → relay probability 1.0  (100%)
Network size ≤ 30  → relay probability 0.85
Network size ≤ 50  → relay probability 0.7
Network size ≤ 100 → relay probability 0.55
Network size > 100 → relay probability 0.4
```

### BitchatMessage inner payload (BitchatMessage.kt) — big-endian
```
[0]     flags (uint8):
          0x01 = isRelay
          0x02 = isPrivate
          0x04 = hasOriginalSender
          0x08 = hasRecipientNickname
          0x10 = hasSenderPeerID
          0x20 = hasMentions
          0x40 = hasChannel
          0x80 = isEncrypted
[1:9]   timestamp (int64 ms since epoch)
[9]     id_len (uint8)
[10..N] id (UTF-8)
[N]     sender_len (uint8)
[N+1..M] sender nickname (UTF-8)
[M:M+2] content_len (uint16)
[M+2..C] content or encryptedContent (UTF-8 / bytes)
optional (flags-gated):
  originalSender:    uint8 len + UTF-8
  recipientNickname: uint8 len + UTF-8
  senderPeerID:      uint8 len + UTF-8
  mentions:          uint8 count + (uint8 len + UTF-8) × count
  channel:           uint8 len + UTF-8
```

---

## How to check for upstream changes

```bash
# 1. Pull latest snapshots of tracked Android files
./upstream/scripts/fetch-upstream.sh

# 2. Verify Pi constants still match
python3 upstream/scripts/check-compat.py

# 3. Review diffs manually for logic changes
git diff upstream/android/
```

Run this before every release and after any upstream commit appears in the
Android repo's git log for tracked files.

---

## What to do when upstream changes

1. Run `fetch-upstream.sh` — the changed file lands in `upstream/android/`
2. Run `check-compat.py` — it will flag any constant that no longer matches
3. For **constant changes** (UUIDs, packet types, sizes): update `bitchatd/protocol/constants.py` first, then any callers
4. For **logic changes** (relay probabilities, fragmentation algorithm): update the corresponding Pi module and its tests
5. Update the `Last-verified commit` column in this table
6. Commit with message: `sync: update to upstream <short-sha> (<file>)`
