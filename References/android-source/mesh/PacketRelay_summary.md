# PacketRelayManager + PacketProcessor — Relay Logic
Sources:
  - PacketRelayManager.kt
  - PacketProcessor.kt
Fetched: 2026-04-25

## Inbound Processing (PacketProcessor)
- Per-peer coroutine actor for sequential processing (prevents race conditions)
- `handleReceivedPacket()` two-tier dispatch:
  - **Public packets** (process unconditionally): ANNOUNCE, MESSAGE, FILE_TRANSFER, LEAVE, FRAGMENT, REQUEST_SYNC
  - **Private packets** (require address match): NOISE_HANDSHAKE, NOISE_ENCRYPTED
- After processing, calls `packetRelayManager.handlePacketRelay()`

## Relay Decisions (PacketRelayManager)
- TTL decrement: `ttl = (packet.ttl - 1u).toUByte()`; drop if TTL reaches 0
- Source-route loop detection: drop if route contains duplicate hops
- Adaptive relay probability based on connected peer count:
  | Peer count | Relay probability |
  |------------|-------------------|
  | ≤ 3        | 100%              |
  | 4–10       | 85%               |
  | 11–30      | 70%               |
  | 31–100     | 55%               |
  | > 100      | 40%               |
- High TTL packets (≥ 4): always relay regardless of probability

## Forwarding Paths
- `relayPacket()` — broadcast relay to all peers
- `sendToPeer(peerID)` — unicast routing
