# BitChatPi IPC API

The daemon (`server/daemon.py`) exposes a local control interface over a Unix domain socket. Any process on the same machine can connect, send commands, and receive a real-time stream of events from the BLE mesh.

---

## Transport

| Property | Value |
|----------|-------|
| Socket type | Unix domain socket (SOCK_STREAM) |
| Default path | `/root/.config/bitchatd/api.sock` |
| Encoding | UTF-8, newline-delimited JSON — one JSON object per line |
| Direction | Full-duplex: client sends commands, daemon sends events and inline command responses |

The socket path can be overridden at daemon startup; the running path is always printed to the daemon log on startup.

---

## Connection lifecycle

1. Connect to the socket.
2. The daemon accepts multiple simultaneous clients.
3. Send JSON commands (one per line, terminated with `\n`).
4. Read lines from the socket — these are either inline responses to a command you just sent, or pushed events that arrive at any time.
5. There is no authentication or handshake. Disconnect by closing the socket.

The daemon does **not** echo a welcome message on connect. Send `{"cmd":"peers"}` immediately after connecting if you want to know the current peer list.

---

## Commands (client → daemon)

Every command is a JSON object with a `"cmd"` field. Send it as a single line followed by `\n`.

### `peers` — list known peers

```json
{"cmd": "peers"}
```

**Inline response:**

```json
{
  "ok": true,
  "event": "peers",
  "list": [
    {"peer_id": "a1b2c3d4...", "nick": "Alice"},
    {"peer_id": "e5f6a7b8...", "nick": "Bob"}
  ]
}
```

`peer_id` is the sender's BLE identity as a lowercase hex string (32 hex chars = 16 bytes).

---

### `send` — send a private (DM) message

```json
{"cmd": "send", "to": "<peer_id_hex>", "content": "Hello!"}
```

| Field | Type | Description |
|-------|------|-------------|
| `to` | string | Hex peer ID of the recipient |
| `content` | string | UTF-8 message text |

**Inline response (success):**

```json
{"ok": true, "msg_id": "550e8400-e29b-41d4-a716-446655440000"}
```

`msg_id` is a UUID string. Hold onto it — delivery and read receipts reference it in their `"ref"` field.

**Inline response (no Noise session yet):**

```json
{"ok": true, "msg_id": "550e8400-..."}
```

The daemon still returns `ok: true` and queues the message internally. It simultaneously sends a BLE `ANNOUNCE` packet to prompt the recipient's device to initiate the Noise handshake. The message is delivered automatically once the session is established. There is currently no event to inform the client when queued messages are actually sent.

**Inline response (error):**

```json
{"ok": false, "error": "missing 'to' or 'content'"}
```

---

### `broadcast` — send a public mesh message

```json
{"cmd": "broadcast", "content": "Hello everyone!"}
```

| Field | Type | Description |
|-------|------|-------------|
| `content` | string | UTF-8 message text |
| `channel` | string | *(optional, currently unused)* |

Broadcasts are sent as unencrypted `MESSAGE` packets and are visible to all mesh nodes. They do not generate receipts.

**Inline response:**

```json
{"ok": true}
```

No `msg_id` is returned because there are no receipts for broadcasts.

---

### `send_file` — send a binary file to a peer

```json
{"cmd": "send_file", "to": "<peer_id_hex>", "path": "/path/to/file.jpg"}
```

| Field | Type | Description |
|-------|------|-------------|
| `to` | string | Hex peer ID of the recipient |
| `path` | string | Absolute path to the file on the Pi's filesystem |

The daemon reads the file, encrypts it, and sends it over the established Noise session. Large files are automatically fragmented.

**Inline response (success):**

```json
{"ok": true, "msg_id": "550e8400-..."}
```

**Inline response (no session):**

```json
{"ok": false, "error": "no established session — send a DM first"}
```

Unlike `send`, file sends are not queued. The recipient must have already opened a DM to establish the Noise session before files can be sent.

**Inline response (file unreadable):**

```json
{"ok": false, "error": "cannot read file: [Errno 2] No such file or directory: '/path/to/file.jpg'"}
```

---

## Events (daemon → client, pushed)

The daemon pushes events to **all** connected clients whenever something happens on the BLE mesh. These arrive as unsolicited lines on the socket, interleaved with command responses. Read them in a separate coroutine or thread.

### `message` — a message was received

```json
{
  "event": "message",
  "from": "a1b2c3d4...",
  "nick": "Alice",
  "content": "Hello!",
  "private": true
}
```

| Field | Type | Description |
|-------|------|-------------|
| `from` | string | Hex peer ID of the sender |
| `nick` | string | Sender's self-reported nickname |
| `content` | string | Message text |
| `private` | boolean | `true` = DM (Noise-encrypted); `false` = public broadcast |

---

### `peer` — a peer appeared or disappeared

```json
{"event": "peer", "action": "seen", "peer_id": "a1b2c3d4...", "nick": "Alice"}
{"event": "peer", "action": "lost", "peer_id": "a1b2c3d4...", "nick": "Alice"}
```

`seen` fires when a new `ANNOUNCE` packet is received from a previously unknown peer.  
`lost` is not currently emitted (the daemon has no timeout-based disconnect detection yet).

---

### `receipt` — delivery or read receipt for a message you sent

```json
{"event": "receipt", "type": "delivery", "ref": "550e8400-...", "from": "a1b2c3d4..."}
{"event": "receipt", "type": "read",     "ref": "550e8400-...", "from": "a1b2c3d4..."}
```

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | `"delivery"` or `"read"` |
| `ref` | string | The `msg_id` returned when the message was sent |
| `from` | string | Hex peer ID that sent the receipt |

The Pi sends both receipt types immediately after decrypting a received message (delivery and read are sent together, since the Pi has no concept of "unread"). Phones follow the same convention.

---

### `file` — a file was received from a peer

```json
{
  "event": "file",
  "from": "a1b2c3d4...",
  "nick": "Alice",
  "path": "/tmp/bitchat_550e8400.jpg",
  "mime": "image/jpeg",
  "name": "photo.jpg"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `from` | string | Hex peer ID of the sender |
| `nick` | string | Sender's nickname |
| `path` | string | Absolute path where the daemon saved the file on the Pi |
| `mime` | string | MIME type detected from file magic bytes |
| `name` | string | Original filename from the file transfer packet (may be synthetic like `file.jpg` if not provided) |

Files are saved to `/tmp/bitchat_<8-char-id><ext>`. The daemon sends a delivery receipt to the sender automatically.

---

## Error response

Any command that the daemon cannot process returns:

```json
{"ok": false, "error": "human-readable reason"}
```

Invalid JSON or a missing `"cmd"` field also triggers this response.

---

## Minimal Python client example

```python
import asyncio, json

SOCK = "/root/.config/bitchatd/api.sock"

async def main():
    reader, writer = await asyncio.open_unix_connection(SOCK)

    # Ask for peer list
    writer.write(b'{"cmd":"peers"}\n')
    await writer.drain()

    # Read events and responses forever
    while True:
        raw = await reader.readline()
        if not raw:
            print("disconnected")
            break
        obj = json.loads(raw)
        ev = obj.get("event")

        if ev == "peers":
            for p in obj.get("list", []):
                print(f"peer: {p['nick']} ({p['peer_id']})")

        elif ev == "message":
            src = "DM" if obj.get("private") else "broadcast"
            print(f"[{src}] {obj['nick']}: {obj['content']}")

        elif ev == "peer":
            print(f"peer {obj['action']}: {obj['nick']}")

        elif ev == "receipt":
            print(f"receipt {obj['type']} for {obj['ref'][:8]}… from {obj['from'][:8]}…")

        elif ev == "file":
            print(f"file from {obj['nick']}: {obj['path']} ({obj['mime']})")

        elif "ok" in obj:
            if obj["ok"]:
                print(f"ok  msg_id={obj.get('msg_id', '—')}")
            else:
                print(f"error: {obj.get('error')}")

asyncio.run(main())
```

---

## Sending a message — full flow

```
client                       daemon                        BLE mesh
  │                             │                              │
  │  {"cmd":"send","to":…}      │                              │
  │ ──────────────────────────► │                              │
  │                             │   NOISE_ENCRYPTED packet     │
  │  {"ok":true,"msg_id":"X"}   │ ──────────────────────────► │
  │ ◄────────────────────────── │                              │
  │                             │   receipt from phone         │
  │  {"event":"receipt",        │ ◄────────────────────────── │
  │   "type":"delivery",        │                              │
  │   "ref":"X",…}              │                              │
  │ ◄────────────────────────── │                              │
  │                             │                              │
  │  {"event":"receipt",        │                              │
  │   "type":"read","ref":"X"}  │                              │
  │ ◄────────────────────────── │                              │
```

If no Noise session exists when `send` is issued, the daemon queues the message and sends an `ANNOUNCE` over BLE to invite the peer to initiate the handshake. The flow then continues from "NOISE_ENCRYPTED packet" once the handshake completes. The client sees `ok: true` immediately either way.

---

## Notes for client authors

- **Multiple clients are supported.** All pushed events go to every connected client simultaneously. A second monitoring client will see all traffic without interfering with the primary UI client.
- **No session tracking in the API.** The daemon manages Noise sessions internally. Clients do not need to know whether a session exists; just send `send` and the daemon handles queuing or immediate delivery.
- **`msg_id` format.** Always a standard UUID v4 string (`xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx`). Receipts reference it verbatim in their `"ref"` field.
- **Files are saved by the daemon.** When a `file` event arrives, the file is already on disk at `path`. The client only needs to display or open it.
- **Peer IDs are stable.** They are derived from the node's Ed25519 identity keypair and persist across restarts (stored in `~/.config/bitchatd/identity.json`). You can use them as stable keys for per-peer state.
