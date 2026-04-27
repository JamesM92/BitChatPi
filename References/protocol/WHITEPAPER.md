# BitChat Protocol Whitepaper
Source: https://github.com/permissionlesstech/bitchat/blob/main/WHITEPAPER.md
Fetched: 2026-04-25

---

## Protocol Design
BitChat implements a **four-layer architecture**: Application, Session, Encryption, and Transport layers. The framework establishes peer-to-peer messaging without central servers, prioritizing "secure, private, and censorship-resistant communication" across ad-hoc networks.

## Message Format
**BitchatPacket** structure includes:
- Fixed 13-byte header (version, type, TTL, timestamp, flags, payload length)
- Variable fields: sender ID, optional recipient ID, payload, optional signature
- Packets padded to 256/512/1024/2048-byte blocks using PKCS#7-style encryption to obscure message length

**BitchatMessage** payload contains: flags, timestamp, UUID, sender nickname, UTF-8 content, and optional original sender/recipient nicknames.

## Bluetooth Mesh Architecture
The protocol uses **Bloom filter-based gossip flooding**: peers relay packets through the network, checking an OptimizedBloomFilter to prevent loops. Packets decrement a TTL field at each hop; relaying halts when TTL reaches zero.

## Encryption Scheme
**Noise_XX_25519_ChaChaPoly_SHA256** protocol provides:
- Mutual authentication via three-message handshake (XX pattern)
- Curve25519 for Diffie-Hellman exchanges
- ChaCha20-Poly1305 for AEAD encryption
- SHA-256 hashing
- Forward secrecy through ephemeral key exchanges

## Relay/Broadcast Mechanism
- **Private messages**: encrypted end-to-end; relays forward without decryption access
- **Broadcast messages**: special recipientID (0xFFFFFFFFFFFFFFFF); relayed network-wide
- **Delivery Acknowledgments**: recipients send DeliveryAck packets confirming receipt

## Identity Management
- Noise static key pair (Curve25519) for session establishment
- Ed25519 signing key pair for announcements
- Fingerprint = SHA-256(static public key)
- Out-of-band fingerprint verification enables social trust layer

## Telemetry/Internet Dependencies
The whitepaper contains no mention of telemetry, analytics, or internet-dependent features. The protocol is designed for offline, mesh-based operation.
