# Contributing to BitChatPi

## Dev environment setup

Requires Python 3.11+ and a Linux host with BlueZ available (or just a Python install for running tests).

```bash
git clone https://github.com/JamesM92/BitChatPi.git
cd BitChatPi
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

## Running the tests

```bash
pytest tests/ -v
```

Tests cover the packet codec, Noise crypto, relay engine, fragment reassembly, and protocol compatibility. They do not require Bluetooth hardware.

## Running the daemon locally

For interactive dev/debug work, use the helper script (stops the systemd service, restarts it on exit):

```bash
sudo ./start-server.sh
```

Or run directly:

```bash
sudo .venv/bin/python3 server/daemon.py --nick DevNode
```

## Project layout

```
server/                 — daemon and bitchatd library
  daemon.py             — entry point
  bitchatd/
    protocol/           — wire format (codec, constants, packet)
    crypto/             — Noise XX session, Ed25519 identity
    ble/                — BlueZ D-Bus GATT + LE advertiser + scanner
    mesh/               — relay engine, fragment reassembly, session manager
    api/                — Unix socket IPC server

client/                 — reference TUI (not polished, for dev/testing)
tests/                  — pytest suite
References/             — research notes and protocol docs (not shipped)
upstream/               — snapshots of BitChat Android source
```

## Submitting changes

- Open an issue first for non-trivial changes so the direction can be agreed on.
- Keep PRs focused — one logical change per PR.
- Run `pytest tests/` before opening a PR; CI will also run it.
- Follow the existing code style (no formatter is enforced).

## Protocol compatibility

The wire format must remain compatible with the BitChat Android app. Before changing anything in `server/bitchatd/protocol/`, check the reference snapshots in `upstream/android/` and the docs in `References/`.
