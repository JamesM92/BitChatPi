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
2. The daemon immediately sends a `hello` event with the node's own peer ID and nickname.
3. Send JSON commands (one per line, terminated with `\n`).
4. Read lines from the socket — these are either inline responses to a command you just sent, or pushed events that arrive at any time.
5. There is no authentication or handshake. Disconnect by closing the socket.

The daemon accepts multiple simultaneous clients. All pushed events are broadcast to every connected client.

---

## Commands (client → daemon)

Every command is a JSON object with a `"cmd"` field. Send it as a single line followed by `\n`.

### `ping` — health check

```json
{"cmd": "ping"}
```

**Inline response:**

```json
{"ok": true, "pong": true}
```

---

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
    {"peer_id": "a1b2c3d4...", "nick": "Alice", "last_seen": 1714236000},
    {"peer_id": "e5f6a7b8...", "nick": "Bob",   "last_seen": 1714235980}
  ]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `peer_id` | string | Hex peer ID (32 hex chars = 16 bytes) |
| `nick` | string | Peer's self-reported nickname |
| `last_seen` | number | Unix timestamp of the last received ANNOUNCE from this peer |

---

### `set_nick` — change the node's nickname

```json
{"cmd": "set_nick", "nick": "MyNode"}
```

**Inline response:**

```json
{"ok": true, "nick": "MyNode"}
```

The new nickname is used in all subsequent ANNOUNCE packets broadcast over the mesh.

---

### `send` — send a private (DM) message

```json
{"cmd": "send", "to": "<peer_id_hex>", "content": "Hello!"}
```

| Field | Type | Description |
|-------|------|-------------|
| `to` | string | Hex peer ID of the recipient |
| `content` | string | UTF-8 message text |

**Inline response:**

```json
{"ok": true, "msg_id": "550e8400-e29b-41d4-a716-446655440000"}
```

`msg_id` is a UUID string. Hold onto it — delivery and read receipts reference it in their `"ref"` field.

If no Noise session exists yet, the daemon returns `ok: true`, queues the message internally, and sends a BLE `ANNOUNCE` to prompt the peer to initiate the handshake. The message is delivered automatically once the session is established.

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

Broadcasts are sent as unencrypted `MESSAGE` packets and are visible to all mesh nodes. They do not generate receipts.

**Inline response:**

```json
{"ok": true, "msg_id": "550e8400-..."}
```

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

Unlike `send`, file sends are not queued. A Noise session must already be established before sending a file.

**Inline response (file unreadable):**

```json
{"ok": false, "error": "cannot read file: [Errno 2] No such file or directory: '...'"}
```

---

## Events (daemon → client, pushed)

The daemon pushes events to all connected clients whenever something happens on the BLE mesh. These arrive as unsolicited lines on the socket, interleaved with command responses. Read them in a separate coroutine or thread.

### `hello` — node identity (sent on connect)

```json
{"event": "hello", "peer_id": "8d524fa0...", "nick": "BitChatPi"}
```

Sent immediately when a client connects. Contains the daemon's own peer ID and current nickname. Use this to distinguish messages the node sent itself from messages received from other peers.

| Field | Type | Description |
|-------|------|-------------|
| `peer_id` | string | This node's hex peer ID |
| `nick` | string | This node's current nickname |

---

### `message` — a message was received

```json
{
  "event": "message",
  "from": "a1b2c3d4...",
  "nick": "Alice",
  "content": "Hello!",
  "private": true,
  "self": false,
  "ts": 1714236000,
  "msg_id": "550e8400-..."
}
```

| Field | Type | Description |
|-------|------|-------------|
| `from` | string | Hex peer ID of the sender |
| `nick` | string | Sender's self-reported nickname |
| `content` | string | Message text |
| `private` | boolean | `true` = DM (Noise-encrypted); `false` = public broadcast |
| `self` | boolean | `true` if this echo is for a message sent by this node |
| `ts` | number | Unix timestamp from the packet header |
| `msg_id` | string | Message UUID (for receipt correlation) |

---

### `peer` — a peer appeared or disappeared

```json
{"event": "peer", "action": "seen", "peer_id": "a1b2c3d4...", "nick": "Alice", "last_seen": 1714236000}
{"event": "peer", "action": "lost", "peer_id": "a1b2c3d4...", "nick": "Alice"}
```

`seen` fires when an `ANNOUNCE` is received from a previously unknown peer.

`lost` fires when a `LEAVE` packet is received from a peer (e.g. the BitChat app backgrounded or the phone left the mesh). It does not fire on timeout — a peer that goes silent without sending `LEAVE` stays in the known-peers list until the daemon is restarted.

| Field | Type | Description |
|-------|------|-------------|
| `action` | string | `"seen"` or `"lost"` |
| `peer_id` | string | Hex peer ID |
| `nick` | string | Peer's nickname |
| `last_seen` | number | Unix timestamp of last ANNOUNCE *(present on `seen` only)* |

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
  "path": "/root/.config/bitchatd/files/bitchat_550e8400.jpg",
  "mime": "image/jpeg",
  "name": "photo.jpg"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `from` | string | Hex peer ID of the sender |
| `nick` | string | Sender's nickname |
| `path` | string | Absolute path where the daemon saved the file (`~/.config/bitchatd/files/`) |
| `mime` | string | MIME type detected from file magic bytes |
| `name` | string | Original filename (may be synthetic like `file.jpg` if not provided by sender) |

The daemon saves the file to disk before emitting this event and sends a delivery+read receipt to the sender automatically.

---

### `fragment_partial` — a large file arrived incomplete

Emitted when a fragmented transfer times out before all pieces arrive. The file was **not** saved — no `file` event will follow for this attempt. The daemon caches the received fragments for up to 60 minutes; if the sender retransmits, the new attempt inherits the cached pieces and may complete without needing to resend everything.

After emitting this event the daemon automatically sends a DM reply to the sender describing what is missing (rate-limited to once per minute per peer).

```json
{
  "event":             "fragment_partial",
  "from":              "a1b2c3d4...",
  "nick":              "Alice",
  "received":          136,
  "total":             137,
  "missing":           [116],
  "combined_received": 136,
  "combined_missing":  [116],
  "attempt":           1,
  "transfer_id":       "c1b4ab17",
  "content_id":        "abc123",
  "approx_kb":         52
}
```

| Field | Type | Description |
|-------|------|-------------|
| `from` | string | Hex peer ID of the sender |
| `nick` | string | Sender's nickname |
| `received` | int | Fragments that arrived in this attempt |
| `total` | int | Total fragments expected |
| `missing` | array of int | Zero-based indices missing from this attempt |
| `combined_received` | int | Fragments held across all attempts (this attempt + rescue cache) |
| `combined_missing` | array of int | Indices still missing after combining all cached fragments |
| `attempt` | int | Attempt number (1 = first send, 2 = first retry, …) |
| `transfer_id` | string | First 8 hex chars of the fragment set ID |
| `content_id` | string | Opaque content identifier set by the sender (may be empty) |
| `approx_kb` | int | Estimated file size in KB |

Retransmission by the sender is automatic but may take several minutes depending on the BitChat app's retry schedule.

---

### `fragment_set_started` — a retransmission began with rescued fragments

Emitted when a new fragment set arrives and the daemon can seed it with cached fragments from a previous partial attempt. This signals that a retry is underway and already has a head start.

```json
{
  "event":      "fragment_set_started",
  "from":       "a1b2c3d4...",
  "nick":       "Alice",
  "total":      137,
  "attempt":    2,
  "inherited":  136,
  "content_id": "abc123"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `from` | string | Hex peer ID of the sender |
| `nick` | string | Sender's nickname |
| `total` | int | Total fragments expected |
| `attempt` | int | Attempt number for this set (≥ 2 when inherited fragments are present) |
| `inherited` | int | Number of fragments pre-loaded from the rescue cache |
| `content_id` | string | Opaque content identifier (may be empty) |

---

### `fragment_completed` — a multi-attempt transfer finished reassembly

Emitted when a fragmented transfer that required at least two attempts finally reassembles successfully. Fired immediately before the `file` event for the same transfer so clients can correlate the completion with earlier `fragment_partial` events.

```json
{
  "event":      "fragment_completed",
  "from":       "a1b2c3d4...",
  "nick":       "Alice",
  "attempt":    2,
  "total":      137,
  "approx_kb":  52,
  "content_id": "abc123"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `from` | string | Hex peer ID of the sender |
| `nick` | string | Sender's nickname |
| `attempt` | int | Attempt number on which reassembly succeeded |
| `total` | int | Total fragment count |
| `approx_kb` | int | Estimated file size in KB |
| `content_id` | string | Opaque content identifier (may be empty) |

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

    # hello arrives automatically on connect
    # then ask for the current peer list
    writer.write(b'{"cmd":"peers"}\n')
    await writer.drain()

    while True:
        raw = await reader.readline()
        if not raw:
            print("disconnected")
            break
        obj = json.loads(raw)
        ev = obj.get("event")

        if ev == "hello":
            print(f"connected as {obj['nick']} ({obj['peer_id'][:8]}…)")

        elif ev == "peers":
            for p in obj.get("list", []):
                print(f"peer: {p['nick']} ({p['peer_id'][:8]}…)")

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
  │  (connect)                  │                              │
  │ ──────────────────────────► │                              │
  │  {"event":"hello",…}        │                              │
  │ ◄────────────────────────── │                              │
  │                             │                              │
  │  {"cmd":"send","to":…}      │                              │
  │ ──────────────────────────► │                              │
  │                             │   NOISE_ENCRYPTED packet     │
  │  {"ok":true,"msg_id":"X"}   │ ──────────────────────────► │
  │ ◄────────────────────────── │                              │
  │                             │   receipt from phone         │
  │  {"event":"receipt",        │ ◄────────────────────────── │
  │   "type":"delivery",…}      │                              │
  │ ◄────────────────────────── │                              │
  │  {"event":"receipt",        │                              │
  │   "type":"read",…}          │                              │
  │ ◄────────────────────────── │                              │
```

If no Noise session exists when `send` is issued, the daemon queues the message and sends an `ANNOUNCE` over BLE to invite the peer to initiate the handshake. The flow continues from "NOISE_ENCRYPTED packet" once the handshake completes. The client sees `ok: true` immediately either way.

---

## Notes for client authors

- **Multiple clients are supported.** All pushed events go to every connected client simultaneously.
- **No session tracking in the API.** The daemon manages Noise sessions internally. Just send `send` — the daemon handles queuing or immediate delivery.
- **`msg_id` format.** Always a standard UUID v4 string. Receipts reference it verbatim in their `"ref"` field.
- **Files are saved by the daemon.** When a `file` event arrives, the file is already on disk at `path`. The client only needs to display or open it.
- **Peer IDs are stable.** Derived from the node's Ed25519 keypair and persist across restarts (`~/.config/bitchatd/identity.json`). Safe to use as stable keys for per-peer state.
