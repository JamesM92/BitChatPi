# BluetoothMeshService.kt — Architecture Summary
Source: https://raw.githubusercontent.com/permissionlesstech/bitchat-android/main/app/src/main/java/com/bitchat/android/mesh/BluetoothMeshService.kt
Fetched: 2026-04-25

## Role
High-level mesh coordinator. Delegates all BLE hardware work to sub-managers.
Does NOT contain UUID definitions or raw byte transmission.

## Delegate Interface
```kotlin
interface BluetoothConnectionManagerDelegate {
    fun onPacketReceived(packet: BitchatPacket, peerID: String, device: BluetoothDevice?)
    fun onDeviceConnected(device: BluetoothDevice)
    fun onDeviceDisconnected(device: BluetoothDevice)
    fun onRSSIUpdated(deviceAddress: String, rssi: Int)
}
```

## Packet Construction
```kotlin
BitchatPacket(
    version    = 1u or 2u,
    type       = MessageType.NOISE_HANDSHAKE / MESSAGE / ANNOUNCE / etc,
    senderID   = hexStringToByteArray(myPeerID),  // 8 bytes
    recipientID = SpecialRecipients.BROADCAST or peer ID,
    timestamp  = System.currentTimeMillis().toULong(),
    payload    = ByteArray,
    signature  = ByteArray?,
    ttl        = MAX_TTL   // 7
)
```

## Outbound Paths
- `connectionManager.broadcastPacket(RoutedPacket)` — fire-and-forget to all peers
- `connectionManager.sendPacketToPeer(peerID, packet)` — unicast

## Sub-Components
- `BluetoothConnectionManager` — BLE hardware, GATT ops
- `PacketProcessor` — inbound routing
- `FragmentManager` — MTU-based splitting
- `PacketRelayManager` — relay decisions, TTL, deduplication
