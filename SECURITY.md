# Security Policy

## Supported versions

Only the latest release is actively maintained.

| Version | Supported |
|---------|-----------|
| 1.0.x   | Yes       |

## Scope

BitChatPi handles end-to-end encrypted BLE mesh traffic using Noise XX and Ed25519. Security-relevant components:

- **Noise XX session handshake** (`server/bitchatd/crypto/noise_session.py`)
- **Ed25519 identity and signing** (`server/bitchatd/crypto/identity.py`)
- **Packet relay and TTL logic** (`server/bitchatd/mesh/relay_engine.py`)
- **IPC socket** (`server/bitchatd/api/`) — Unix domain socket, world-writable by default
- **Received file handling** (`server/bitchatd/mesh/session_manager.py`)

Out of scope: the reference TUI client (`client/`), documentation, and test files.

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Report privately by emailing **jamesmanley1992@gmail.com** with:

- A description of the vulnerability and its potential impact
- Steps to reproduce or a proof-of-concept (if available)
- The version or commit you tested against

You will receive an acknowledgement within 48 hours. Fixes are coordinated privately before public disclosure.
