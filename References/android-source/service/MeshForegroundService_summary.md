# MeshForegroundService.kt — Android Service Architecture
Source: https://raw.githubusercontent.com/permissionlesstech/bitchat-android/main/app/src/main/java/com/bitchat/android/service/MeshForegroundService.kt
Fetched: 2026-04-25

## Role
Android foreground service that keeps the Bluetooth mesh running while the app is backgrounded.

## Initialization Flow
1. `onCreate()` — creates notification channel, ensures BluetoothMeshService exists via MeshServiceHolder
2. `onStartCommand(ACTION_START)` — promotes to foreground if permissions granted, calls `meshService.startServices()`
3. `onStartCommand(ACTION_STOP)` — calls `meshService.stopServices()`, removes foreground status
4. `onStartCommand(ACTION_QUIT)` — full app shutdown via `AppShutdownCoordinator`

## Key References
- `MeshServiceHolder.getOrCreate(context)` — singleton BluetoothMeshService
- `meshService.startServices()` / `meshService.stopServices()`
- `meshService.getActivePeerCount()` — shown in notification
- 5-second coroutine polling loop for notification updates

## Pi Equivalent
On the Pi this becomes a systemd service (or daemon process) that:
- Starts on boot
- Manages the BLE GATT server/scanner
- Exposes an IPC socket for local clients

## No Nostr in this file
MeshForegroundService is Bluetooth-only. Nostr is wired elsewhere.
