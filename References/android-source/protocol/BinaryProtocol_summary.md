# BinaryProtocol.kt — Packet Format Reference
Source: https://raw.githubusercontent.com/permissionlesstech/bitchat-android/main/app/src/main/java/com/bitchat/android/protocol/BinaryProtocol.kt
Fetched: 2026-04-25

## Message Types (UByte)
| Name             | Value |
|------------------|-------|
| ANNOUNCE         | 0x01  |
| MESSAGE          | 0x02  |
| LEAVE            | 0x03  |
| NOISE_HANDSHAKE  | 0x10  |
| NOISE_ENCRYPTED  | 0x11  |
| FRAGMENT         | 0x20  |
| REQUEST_SYNC     | 0x21  |
| FILE_TRANSFER    | 0x22  |

## Header Layout

### Version 1 (13 bytes)
| Field         | Size | Type              |
|---------------|------|-------------------|
| Version       | 1    | UByte             |
| Type          | 1    | UByte             |
| TTL           | 1    | UByte             |
| Timestamp     | 8    | ULong (big-endian)|
| Flags         | 1    | UByte             |
| PayloadLength | 2    | UShort (big-endian)|

### Version 2+ (15 bytes)
Same as v1 but PayloadLength is 4 bytes (UInt big-endian).

## Flags Byte
| Bit  | Meaning         |
|------|-----------------|
| 0x01 | HAS_RECIPIENT   |
| 0x02 | HAS_SIGNATURE   |
| 0x04 | IS_COMPRESSED   |
| 0x08 | HAS_ROUTE (v2+) |

## Variable Sections (after header, in order)
1. **SenderID**: 8 bytes fixed
2. **RecipientID**: 8 bytes (if HAS_RECIPIENT)
3. **Route**: 1-byte hop count + N×8 bytes (v2+, if HAS_ROUTE); max 255 hops
4. **Payload**: variable
   - If IS_COMPRESSED: 2–4 byte original-size prefix + compressed data
5. **Signature**: 64 bytes (if HAS_SIGNATURE)

## Special Recipients
- **Broadcast**: `0xFFFFFFFFFFFFFFFF` (8 bytes all 0xFF)

## Padding
PKCS#7-style padding to 256/512/1024/2048-byte block sizes for traffic analysis resistance.

## Constraints
- MAX_PAYLOAD_LENGTH: 10,485,760 bytes
- Compression bomb protection: ratio > 50,000:1 rejected
- Decoder tries raw data first, then padding-stripped

## AppConstants (key values)
- MESSAGE_TTL_HOPS: 7
- SYNC_TTL_HOPS: 0
- MESSAGE_TIMEOUT_MS: 300,000 ms
- Stale peer timeout: 180,000 ms
- RSSI update interval: 5,000 ms
- Fragment timeout: 30,000 ms
- StoreForward cache timeout: 43,200,000 ms (12 hours)
- Max cached messages: 100
- Deduplicator capacity: 10,000
