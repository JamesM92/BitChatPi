# References — BitChatPi Research Archive

Organised by topic. All files were fetched from public sources on 2026-04-25.

## Directory Structure

```
References/
├── protocol/               # BitChat protocol specification
│   └── WHITEPAPER.md       # Full whitepaper (permissionlesstech/bitchat)
│
├── android-source/         # Summaries of key Android source files
│   ├── mesh/
│   │   ├── BluetoothMeshService_summary.md   # Top-level mesh coordinator
│   │   ├── BLE_GATT_details.md               # UUIDs, MTU, fragmentation, GATT setup
│   │   └── PacketRelay_summary.md            # TTL, Bloom filter, relay probability
│   ├── protocol/
│   │   └── BinaryProtocol_summary.md         # Packet binary format, all types/flags
│   ├── nostr/
│   │   └── NostrRelayManager_summary.md      # Internet/Nostr bridge (optional, disableable)
│   └── service/
│       └── MeshForegroundService_summary.md  # Android service → Pi systemd equivalent
│
└── architecture/
    └── BitChatPi_Architecture.md             # Pi client-server design plan
```

## Source Repository
- Android: https://github.com/permissionlesstech/bitchat-android
- Whitepaper: https://github.com/permissionlesstech/bitchat/blob/main/WHITEPAPER.md
