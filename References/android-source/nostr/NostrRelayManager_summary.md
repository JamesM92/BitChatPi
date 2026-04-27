# NostrRelayManager.kt — Internet / Nostr Integration
Source: https://raw.githubusercontent.com/permissionlesstech/bitchat-android/main/app/src/main/java/com/bitchat/android/nostr/NostrRelayManager.kt
Fetched: 2026-04-25

## Purpose
Optional internet bridge that publishes/subscribes BitChat messages to/from the Nostr protocol
over WebSocket connections to public relays. This is the PRIMARY internet dependency in BitChat.

## Default Relay URLs (hardcoded)
- wss://relay.damus.io
- wss://relay.primal.net
- wss://offchain.pub
- wss://nostr21.com
- Additional relays selected dynamically via `RelayDirectory.closestRelaysForGeohash()`

## Data Sent to Internet
- Nostr events (JSON) via `NostrRequest.Event` objects
- Gift-wrapped direct message events (`pendingGiftWrapIDs`)
- Subscription filter queries to relay WebSockets

## WebSocket Transport
- OkHttp `newWebSocket()` with `RelayWebSocketListener`
- Automatic reconnection: exponential backoff, 1s initial → 5m max
- Optional Tor routing via `OkHttpProvider.webSocketClient()` (Arti integration)

## Geohash Integration
- `ensureGeohashRelaysConnected(geohash)` — maps geohash to nearest relays
- `subscribeForGeohash(geohash)` — subscribes to geohash-specific relay subset
- `sendEventToGeohash(event, geohash)` — routes to geohash-mapped relays

## Disabling (for Pi)
All internet connectivity can be disabled by:
- Simply not initialising NostrRelayManager
- Or calling `disconnect()` then never calling `connect()`

The Nostr layer is entirely separate from the Bluetooth mesh — mesh works without it.

## Nostr Directory Structure (nostr/ package)
- Bech32.kt — key encoding
- GeohashAliasRegistry.kt — geohash ↔ Nostr identity mapping
- GeohashConversationRegistry.kt
- GeohashMessageHandler.kt
- GeohashRepository.kt — participant tracking (Nostr-side)
- LocationNotesInitializer.kt / LocationNotesManager.kt
- NostrClient.kt — WebSocket client
- NostrCrypto.kt — secp256k1 for Nostr
- NostrDirectMessageHandler.kt
- NostrEmbeddedBitChat.kt
- NostrEvent.kt / NostrFilter.kt / NostrRequest.kt
- NostrEventDeduplicator.kt
- NostrIdentity.kt
- NostrProofOfWork.kt / PoWPreferenceManager.kt
- NostrProtocol.kt
- NostrRelayManager.kt
- NostrSubscriptionManager.kt
- NostrTestManager.kt
- NostrTransport.kt
- RelayDirectory.kt — maps geohashes to relay URLs
