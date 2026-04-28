# ─── PROTOCOL CONTRACT ────────────────────────────────────────────────────────
# Derived from: app/src/main/java/com/bitchat/android/util/AppConstants.kt
# Last verified against upstream commit: 5b0a7d0 (2026-03-26)
# Run `python3 upstream/scripts/check-compat.py` to verify constants still match.
# The Android repo is the authority — any discrepancy here is a bug in this file.
# ──────────────────────────────────────────────────────────────────────────────

# ── BLE GATT UUIDs ────────────────────────────────────────────────────────────
# AppConstants.Mesh.Gatt
SERVICE_UUID        = "F47B5E2D-4A9E-4C5A-9B3F-8E1D2C3A4B5C"
CHARACTERISTIC_UUID = "A1B2C3D4-E5F6-4A5B-8C9D-0E1F2A3B4C5D"
DESCRIPTOR_UUID     = "00002902-0000-1000-8000-00805F9B34FB"

# ── Packet types ──────────────────────────────────────────────────────────────
# app/src/main/java/com/bitchat/android/protocol/BinaryProtocol.kt
class MessageType:
    ANNOUNCE        = 0x01
    MESSAGE         = 0x02
    LEAVE           = 0x03
    NOISE_HANDSHAKE = 0x10
    NOISE_ENCRYPTED = 0x11
    FRAGMENT        = 0x20
    REQUEST_SYNC    = 0x21
    FILE_TRANSFER   = 0x22

# ── Packet flags byte ─────────────────────────────────────────────────────────
# BinaryProtocol.kt
class PacketFlags:
    HAS_RECIPIENT = 0x01
    HAS_SIGNATURE = 0x02
    IS_COMPRESSED = 0x04
    HAS_ROUTE     = 0x08  # v2+ only

# ── Header sizes ──────────────────────────────────────────────────────────────
# BinaryProtocol.kt
HEADER_V1_SIZE = 14  # version:1 + type:1 + ttl:1 + timestamp:8 + flags:1 + payloadLen:2
HEADER_V2_SIZE = 16  # same but payloadLen is 4 bytes (uint32)

# ── Sender / recipient ────────────────────────────────────────────────────────
PEER_ID_SIZE      = 8
BROADCAST_ID      = bytes([0xFF] * 8)

# ── Padding block sizes ───────────────────────────────────────────────────────
PADDING_BLOCK_SIZES = (256, 512, 1024, 2048)

# ── TTL ───────────────────────────────────────────────────────────────────────
# AppConstants
MESSAGE_TTL_HOPS = 7   # default TTL for new messages
SYNC_TTL_HOPS    = 0   # TTL for neighbour-only sync packets

# ── Fragmentation ─────────────────────────────────────────────────────────────
# AppConstants.Fragmentation  +  FragmentPayload.kt
FRAGMENT_SIZE_THRESHOLD   = 512       # fragment if encoded packet > this (bytes)
MAX_FRAGMENT_SIZE         = 469       # max data bytes per chunk
FRAGMENT_TIMEOUT_MS       = 300_000   # retained for reference; no longer used — expiry is now
                                      # inactivity-based (30 s floor + total/30*1.5 s)
FRAGMENT_CLEANUP_MS       = 10_000    # how often to run cleanup
MAX_FRAGMENTS_PER_ID      = 256
MAX_FRAGMENT_TOTAL_BYTES  = 1_048_576 # per fragment set
MAX_ACTIVE_FRAGMENT_SETS  = 64
MAX_GLOBAL_FRAGMENT_BYTES = 4 * 1_048_576

FRAGMENT_HEADER_SIZE      = 13        # FragmentPayload.HEADER_SIZE
FRAGMENT_ID_SIZE          = 8         # FragmentPayload.FRAGMENT_ID_SIZE

# ── Relay probabilities ───────────────────────────────────────────────────────
# PacketRelayManager.kt — exact values; do not adjust without upstream change
RELAY_HIGH_TTL_THRESHOLD = 4     # TTL >= this → always relay
RELAY_SMALL_NET_MAX      = 3     # peer count <= this → always relay
RELAY_PROB_LE10          = 1.0   # peer count 4–10
RELAY_PROB_LE30          = 0.85  # peer count 11–30
RELAY_PROB_LE50          = 0.7   # peer count 31–50
RELAY_PROB_LE100         = 0.55  # peer count 51–100
RELAY_PROB_LARGE         = 0.4   # peer count > 100

# ── Security / deduplication ──────────────────────────────────────────────────
# AppConstants.Security
MESSAGE_TIMEOUT_MS        = 300_000
MAX_PROCESSED_MESSAGES    = 10_000
MAX_PROCESSED_KEY_EXCHANGES = 1_000

# ── Compression ───────────────────────────────────────────────────────────────
# AppConstants.Protocol
COMPRESSION_THRESHOLD_BYTES = 100
MAX_PAYLOAD_LENGTH          = 10_485_760
MAX_COMPRESSION_RATIO       = 50_000   # reject decompressed output > input * this

# ── Noise ─────────────────────────────────────────────────────────────────────
# AppConstants.Noise
NOISE_REKEY_TIME_MS           = 3_600_000   # 1 hour
NOISE_REKEY_MSG_LIMIT_SESSION = 10_000
NOISE_MAX_PAYLOAD_BYTES       = 256

# ── Peer lifecycle ────────────────────────────────────────────────────────────
# AppConstants.Mesh
STALE_PEER_TIMEOUT_MS   = 180_000  # evict peer after this with no activity
PEER_CLEANUP_INTERVAL_MS = 60_000
RSSI_UPDATE_INTERVAL_MS  = 5_000
MAX_CONNECTIONS_NORMAL   = 8

# ── Store-and-forward ─────────────────────────────────────────────────────────
# AppConstants.StoreForward
STORE_FORWARD_CACHE_TIMEOUT_MS    = 43_200_000  # 12 hours
MAX_CACHED_MESSAGES               = 100
MAX_CACHED_MESSAGES_FAVORITES     = 1_000
STORE_FORWARD_CLEANUP_INTERVAL_MS = 600_000

# ── Nickname ──────────────────────────────────────────────────────────────────
# AppConstants.UI
MAX_NICKNAME_LENGTH = 15
