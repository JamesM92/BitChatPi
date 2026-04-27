# BLE GATT Implementation Details
Sources:
  - BluetoothGattServerManager.kt
  - BluetoothGattClientManager.kt
  - BluetoothPacketBroadcaster.kt
  - FragmentManager.kt
  - AppConstants.kt
Fetched: 2026-04-25

## UUIDs
| Name              | Value                                  |
|-------------------|----------------------------------------|
| Service UUID      | F47B5E2D-4A9E-4C5A-9B3F-8E1D2C3A4B5C |
| Characteristic UUID | A1B2C3D4-E5F6-4A5B-8C9D-0E1F2A3B4C5D |
| Descriptor UUID (CCCD) | 00002902-0000-1000-8000-00805f9b34fb |

## Characteristic Properties
`PROPERTY_READ | PROPERTY_WRITE | PROPERTY_WRITE_NO_RESPONSE | PROPERTY_NOTIFY`

## Server Setup
- Registers GATT service with above UUIDs
- Parses inbound writes: `BitchatPacket.fromBinaryData(value)`
- Sender ID = first 8 bytes of packet, hex-formatted
- Advertising scan response includes first 8 bytes of peerID as service data
  (enables deduplication even when MAC address rotates)

## Client Setup
1. Scan for devices advertising Service UUID
2. Connect: `device.connectGatt(context, false, gattCallback, TRANSPORT_LE)`
3. Request MTU 517 before service discovery
4. Discover services → locate Characteristic UUID
5. Enable notifications: `setCharacteristicNotification(char, true)`
6. Write CCCD descriptor: `ENABLE_NOTIFICATION_VALUE`
7. Receive data in `onCharacteristicChanged()` → `BitchatPacket.fromBinaryData(value)`

## Fragmentation
- Threshold: packets > 512 bytes are fragmented
- Max fragment payload: **469 bytes** (512 MTU minus overhead)
- Fragment header: **13 bytes**
  - 8 bytes: random fragment ID
  - 4 bytes: index + total count
  - 1 byte: original message type
- 20ms delay between successive fragment writes
- Reassembly timeout: 30 seconds
- Fragments purged after 30s to prevent memory exhaustion

## Broadcast vs Unicast
- Broadcast: sends to all connected server + client connections, skipping sender and last relayer
- Unicast: `recipientID != 0xFFFFFFFFFFFFFFFF` → search server connections first, then client
- Source-routed: explicit route in packet → send only to first hop

## Transmission
- Server→Client: `gattServer.notifyCharacteristicChanged(device, characteristic, false)`
- Client→Server: `characteristic.value = data; gatt.writeCharacteristic(characteristic)`
