// upstream snapshot — DO NOT EDIT
// source: app/src/main/java/com/bitchat/android/model/BitchatMessage.kt
// commit: 633a50675392f340167fc62b18e611920398d50e  (2025-09-19)
// fetched: 2026-04-25
package com.bitchat.android.model

// Key binary serialisation layout for toBinaryPayload() / fromBinaryPayload()
// (full Android+Parcelable boilerplate omitted; byte layout is what matters for Pi)
//
// NOTE (2026-04-25): Observed on-wire format differs from this snapshot.
// Actual format (confirmed by decrypting live packets):
//   flags(1) | id_len(uint16) | id | msg_type(uint8)=1 | content_len(uint8) | content
//   Optional tail (FLAG_HAS_SENDER_PEER): peer_id_len(1) | peer_id_hex
//   NO timestamp field. NO sender name field. id_len is uint16 not uint8.
//   This snapshot (commit 633a50675392f340167fc62b18e611920398d50e, 2025-09-19) is STALE.
//
// toBinaryPayload() layout (big-endian throughout) — STALE, see note above:
//
//   Offset  Size  Field
//   ------  ----  -----
//   0       1     flags (UByte)
//                   0x01 = isRelay
//                   0x02 = isPrivate
//                   0x04 = hasOriginalSender
//                   0x08 = hasRecipientNickname
//                   0x10 = hasSenderPeerID
//                   0x20 = hasMentions
//                   0x40 = hasChannel
//                   0x80 = isEncrypted
//   1       8     timestamp (Long, ms since epoch, big-endian)
//   9       1     id_len (UByte, max 255)
//   10      N     id (UTF-8, N = id_len)
//   10+N    1     sender_len (UByte, max 255)
//   11+N    M     sender (UTF-8, M = sender_len)
//   ...     2     content_len (UShort, max 65535, big-endian)
//   ...     C     content or encryptedContent (UTF-8 / raw bytes, C = content_len)
//
//   Optional fields (present only when corresponding flag is set):
//     originalSender:      1-byte len + UTF-8 bytes
//     recipientNickname:   1-byte len + UTF-8 bytes
//     senderPeerID:        1-byte len + UTF-8 bytes
//     mentions:            1-byte count + (1-byte len + UTF-8 bytes) × count
//     channel:             1-byte len + UTF-8 bytes
//
// Data class fields:
//   id: String
//   sender: String                   (nickname)
//   content: String
//   type: BitchatMessageType         (Message | Audio | Image | File)
//   timestamp: Date
//   isRelay: Boolean
//   originalSender: String?
//   isPrivate: Boolean
//   recipientNickname: String?
//   senderPeerID: String?
//   mentions: List<String>?
//   channel: String?
//   encryptedContent: ByteArray?
//   isEncrypted: Boolean
//   deliveryStatus: DeliveryStatus?
//   powDifficulty: Int?
