"""
Unix-socket IPC server — newline-delimited JSON.

Client → daemon (commands):
  {"cmd": "ping"}
  {"cmd": "set_nick",  "nick": "NodeBot"}
  {"cmd": "send",      "to": "<peer_id_hex>", "content": "..."}
  {"cmd": "broadcast", "content": "..."}
  {"cmd": "send_file", "to": "<peer_id_hex>", "path": "..."}
  {"cmd": "peers"}

Daemon → all clients (pushed events):
  {"event": "hello",   "peer_id": "...", "nick": "..."}
      — sent immediately on connect so clients know their own address/nick
  {"event": "message", "from": "<peer_id_hex>", "nick": "...", "content": "...",
                        "private": true|false, "self": false|true,
                        "ts": <unix_seconds>, "msg_id": "<uuid>"}
      — self=true when the message originated from this IPC client (sent echo)
  {"event": "receipt", "type": "delivery"|"read", "ref": "<uuid>", "from": "<peer_id_hex>"}
  {"event": "peer",    "action": "seen"|"lost", "peer_id": "...", "nick": "...",
                        "last_seen": <unix_seconds>}
  {"event": "file",    "from": "...", "nick": "...", "path": "...", "mime": "...", "name": "..."}

Each command receives an inline response:
  {"ok": true, "msg_id": "<uuid>"}          — send / broadcast / send_file
  {"ok": true, "pong": true}                — ping
  {"ok": true, "nick": "..."}               — set_nick
  {"ok": true, "event": "peers", "list": [{"peer_id": "...", "nick": "...",
                                            "last_seen": <unix_seconds>}, ...]}
  {"ok": false, "error": "<code>"}          — error codes:
      not_ready | unknown_command | invalid_params | invalid_peer_id
      no_session | encrypt_failed | file_read_error
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
from typing import Awaitable, Callable, Optional

HelloProvider = Callable[[], dict]

log = logging.getLogger(__name__)

CommandHandler = Callable[[dict], Awaitable[Optional[dict]]]


class IpcServer:
    """
    Async Unix-socket server.  Call publish() to fan-out an event to every
    connected client.  Register a coroutine with set_command_handler() to
    handle inbound commands and return an optional per-client reply.
    """

    def __init__(self, socket_path: str) -> None:
        self._path = socket_path
        self._server: Optional[asyncio.AbstractServer] = None
        self._clients: list[asyncio.StreamWriter] = []
        self._handler: Optional[CommandHandler] = None
        self._hello_provider: Optional[HelloProvider] = None

    def set_command_handler(self, handler: CommandHandler) -> None:
        self._handler = handler

    def set_hello_provider(self, provider: HelloProvider) -> None:
        """Register a callback that returns the hello event dict sent on every new connection."""
        self._hello_provider = provider

    async def start(self) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        try:
            os.unlink(self._path)
        except FileNotFoundError:
            pass
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=self._path
        )
        log.info("IPC server listening at %s", self._path)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        for w in list(self._clients):
            try:
                w.close()
                await w.wait_closed()
            except Exception:
                pass
        self._clients.clear()
        try:
            os.unlink(self._path)
        except FileNotFoundError:
            pass

    async def publish(self, event: dict) -> None:
        """Push an event to every connected client (fire-and-forget per client)."""
        if not self._clients:
            return
        line = (json.dumps(event) + "\n").encode()
        dead: list[asyncio.StreamWriter] = []
        for w in list(self._clients):
            try:
                w.write(line)
                await w.drain()
            except Exception:
                dead.append(w)
        for w in dead:
            self._clients.discard(w) if hasattr(self._clients, "discard") else None
            try:
                self._clients.remove(w)
            except ValueError:
                pass

    # ── internal ──────────────────────────────────────────────────────────────

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername") or "<unix>"
        log.info("IPC client connected  peer=%s  total=%d", peer, len(self._clients) + 1)
        self._clients.append(writer)
        if self._hello_provider is not None:
            hello = self._hello_provider()
            await self._reply(writer, hello)
        try:
            while True:
                try:
                    raw = await asyncio.wait_for(reader.readline(), timeout=None)
                except asyncio.IncompleteReadError:
                    break
                if not raw:
                    break
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    cmd = json.loads(raw)
                except json.JSONDecodeError as exc:
                    await self._reply(writer, {"ok": False, "error": f"invalid JSON: {exc}"})
                    continue
                if not isinstance(cmd, dict) or "cmd" not in cmd:
                    await self._reply(writer, {"ok": False, "error": "missing 'cmd' field"})
                    continue
                log.debug("IPC cmd=%s from %s", cmd.get("cmd"), peer)
                if self._handler:
                    try:
                        resp = await self._handler(cmd)
                    except Exception as exc:
                        log.exception("IPC command handler raised")
                        resp = {"ok": False, "error": str(exc)}
                    if resp is not None:
                        await self._reply(writer, resp)
                else:
                    await self._reply(writer, {"ok": False, "error": "no handler registered"})
        except Exception:
            log.debug("IPC client read loop ended", exc_info=True)
        finally:
            try:
                self._clients.remove(writer)
            except ValueError:
                pass
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            log.info("IPC client disconnected  remaining=%d", len(self._clients))

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, data: dict) -> None:
        try:
            writer.write((json.dumps(data) + "\n").encode())
            await writer.drain()
        except Exception:
            pass
