#!/usr/bin/env python3
"""
Interactive CLI client for the bitchatd IPC socket.

Usage:
    python3 tools/ipc_client.py [--sock PATH]

Commands (type at the prompt):
    peers                          — list known peers
    send <peer_id_hex> <message>   — send private message
    broadcast <message>            — send public broadcast
    quit / exit / Ctrl-C           — exit

Incoming events are printed as they arrive.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

DEFAULT_SOCK = str(Path.home() / ".config" / "bitchatd" / "api.sock")


async def _reader_loop(r: asyncio.StreamReader) -> None:
    try:
        while True:
            raw = await r.readline()
            if not raw:
                print("\n[disconnected]")
                return
            try:
                obj = json.loads(raw)
                print(f"\r← {json.dumps(obj)}")
            except json.JSONDecodeError:
                print(f"\r← (raw) {raw.decode(errors='replace').rstrip()}")
            print("> ", end="", flush=True)
    except asyncio.CancelledError:
        pass


async def main(sock_path: str) -> None:
    try:
        r, w = await asyncio.open_unix_connection(sock_path)
    except FileNotFoundError:
        print(f"error: socket not found at {sock_path} — is ble_smoke_test.py running?",
              file=sys.stderr)
        sys.exit(1)
    except ConnectionRefusedError:
        print(f"error: connection refused at {sock_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Connected to {sock_path}")
    print("Commands: peers | send <peer_id_hex> <msg> | broadcast <msg> | quit")

    reader_task = asyncio.create_task(_reader_loop(r))

    loop = asyncio.get_event_loop()
    try:
        while True:
            print("> ", end="", flush=True)
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            if line.lower() in ("quit", "exit"):
                break

            parts = line.split(None, 1)
            cmd_name = parts[0].lower()

            if cmd_name == "peers":
                cmd = {"cmd": "peers"}
            elif cmd_name == "send":
                rest = parts[1] if len(parts) > 1 else ""
                toks = rest.split(None, 1)
                if len(toks) < 2:
                    print("usage: send <peer_id_hex> <message>")
                    continue
                cmd = {"cmd": "send", "to": toks[0], "content": toks[1]}
            elif cmd_name == "broadcast":
                content = parts[1] if len(parts) > 1 else ""
                if not content:
                    print("usage: broadcast <message>")
                    continue
                cmd = {"cmd": "broadcast", "content": content}
            else:
                print(f"unknown command: {cmd_name!r}")
                continue

            w.write((json.dumps(cmd) + "\n").encode())
            await w.drain()

    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        reader_task.cancel()
        try:
            w.close()
            await w.wait_closed()
        except Exception:
            pass
        print("\nbye.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sock", default=DEFAULT_SOCK, help="path to api.sock")
    args = ap.parse_args()
    asyncio.run(main(args.sock))
