#!/usr/bin/env python3
"""
urwid TUI client for bitchatd IPC socket.

Layout (click or ↑↓ to navigate peers, Tab cycles panels, Enter sends):
  ┌─ BitChatPi ─────────────────────────────────────────────────────┐
  │ Peers             │ Global Chat                                  │
  │ ─────────────     │ ────────────────────────────────────────     │
  │ ▶ Global          │ [12:34] *** Connected                       │
  │   alice  •        │ [12:35] alice: hello                        │
  │   bob             │ [12:36] me: hi                              │
  ├───────────────────┴─────────────────────────────────────────────┤
  │ > type here and press Enter                                     │
  └─────────────────────────────────────────────────────────────────┘

Keys:
  ↑ / ↓     navigate peer list when peer panel is focused
  Enter      send message (when input bar focused)
  Tab        cycle focus: peers → chat → input
  Click      select peer, or focus any panel
  Ctrl-C     quit

Requirements:
    pip install urwid

Usage:
    python3 tools/tui.py [--sock PATH]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import queue
import re
import sys
import threading
import time
from collections import OrderedDict, defaultdict
from datetime import datetime
from pathlib import Path

try:
    import urwid
except ImportError:
    sys.exit("urwid not found — run:  .venv/bin/pip install urwid")

# Img2ContourAscii color output uses ANSI 24-bit sequences: \033[38;2;R;G;Bm{ch}\033[0m
# Convert them to urwid AttrSpec markup so urwid knows the true display width.
_ANSI_COLOR_RE = re.compile(r'\033\[38;2;(\d+);(\d+);(\d+)m(.)\033\[0m')


def _ansi_to_urwid(line: str) -> list:
    """Parse an Img2ContourAscii colored line into a urwid markup list.

    Merges consecutive same-color characters into single segments so urwid
    processes O(color-changes) segments instead of O(characters) per row.
    """
    markup = []
    pos = 0
    cur_color: str | None = None
    cur_text = ""

    for m in _ANSI_COLOR_RE.finditer(line):
        if m.start() > pos:
            if cur_text:
                markup.append((urwid.AttrSpec(f'#{cur_color}', 'default'), cur_text))
                cur_text = ""
                cur_color = None
            markup.append(line[pos:m.start()])
        r, g, b, ch = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)
        color = f'{r:02x}{g:02x}{b:02x}'
        if color == cur_color:
            cur_text += ch
        else:
            if cur_text:
                markup.append((urwid.AttrSpec(f'#{cur_color}', 'default'), cur_text))
            cur_color = color
            cur_text = ch
        pos = m.end()

    if cur_text:
        markup.append((urwid.AttrSpec(f'#{cur_color}', 'default'), cur_text))
    if pos < len(line):
        markup.append(line[pos:])
    return markup or [line]


DEFAULT_SOCK = str(Path.home() / ".config" / "bitchatd" / "api.sock")
GLOBAL_KEY = "__global__"


def _fmt_ranges(indices: list[int]) -> str:
    """Compact a sorted list of ints into hyphenated ranges: [1,2,3,7] → '1-3, 7'."""
    if not indices:
        return ""
    indices = sorted(indices)
    parts = []
    start = end = indices[0]
    for n in indices[1:]:
        if n == end + 1:
            end = n
        else:
            parts.append(str(start) if start == end else f"{start}-{end}")
            start = end = n
    parts.append(str(start) if start == end else f"{start}-{end}")
    return ", ".join(parts)
PEER_PANEL_W = 22  # peer panel width in columns

PALETTE = [
    ("header",       "black",         "dark cyan",   "bold"),
    ("panel_title",  "light cyan",    "dark blue",   "bold"),
    ("divider",      "dark gray",     ""),
    ("peer_normal",  "white",         ""),
    ("peer_active",  "black",         "light gray",  "bold"),
    ("peer_unread",  "light magenta", "",            "bold"),
    ("peer_focus",   "black",         "white",       "bold"),
    ("chat_self",    "dark cyan",     ""),
    ("chat_other",   "white",         ""),
    ("chat_system",  "yellow",        ""),
    ("chat_receipt", "dark green",    ""),
    ("input_bar",    "white",         "dark blue"),
    ("status_ok",    "dark green",    ""),
    ("status_err",   "light red",     ""),
]


# ── Peer list item ────────────────────────────────────────────────────────────

_FILE_TAG = "\x00FILE\x00"   # internal sentinel stored in chat entry text


class FileButton(urwid.WidgetWrap):
    """Selectable file row — press Enter/Space or click to preview."""

    def __init__(self, prefix: str, path: str, on_open=None) -> None:
        self._path = path
        self._on_open = on_open
        name = os.path.basename(path) or path
        label = f"{prefix}[{name}  — Enter to preview]"
        icon = urwid.SelectableIcon(label, cursor_position=0)
        super().__init__(urwid.AttrMap(icon, "chat_system", "peer_focus"))

    def selectable(self) -> bool:
        return True

    def keypress(self, size, key: str):
        if key in ("enter", " "):
            if self._on_open:
                self._on_open(self._path)
            return None
        return key

    def mouse_event(self, size, event: str, button: int, col: int, row: int,
                    focus: bool) -> bool:
        if "press" in event and button == 1:
            if self._on_open:
                self._on_open(self._path)
            return True
        return False


class PeerButton(urwid.WidgetWrap):
    """Selectable, clickable peer list entry."""

    def __init__(self, peer_id: str, nick: str, active: bool, unread: bool,
                 on_select) -> None:
        self._peer_id = peer_id
        self._on_select = on_select

        if active:
            label = f"▶ {nick}"      # ▶
            normal_attr = "peer_active"
        elif unread:
            label = f"• {nick}"      # •
            normal_attr = "peer_unread"
        else:
            label = f"  {nick}"
            normal_attr = "peer_normal"

        icon = urwid.SelectableIcon(label, cursor_position=0)
        super().__init__(urwid.AttrMap(icon, normal_attr, "peer_focus"))

    def selectable(self) -> bool:
        return True

    def keypress(self, size, key: str):
        if key in ("enter", " "):
            self._on_select(self._peer_id)
            return None
        return key

    def mouse_event(self, size, event: str, button: int, col: int, row: int,
                    focus: bool) -> bool:
        if "press" in event and button == 1:
            self._on_select(self._peer_id)
            return True
        return False


# ── Main application ──────────────────────────────────────────────────────────

class App:
    def __init__(self, sock_path: str) -> None:
        self.sock_path = sock_path
        self.peers: OrderedDict[str, str] = OrderedDict([(GLOBAL_KEY, "Global")])
        # (ts, label, text, palette_attr)
        self.chats: defaultdict[str, list[tuple[str, str, str, str]]] = defaultdict(list)
        self.active = GLOBAL_KEY
        self.unread: set[str] = set()
        self.connected = False

        self.ui_q: queue.Queue = queue.Queue()
        self.ipc_q: queue.Queue = queue.Queue()

        # Correlate sent-message UUIDs with their chat entry for inline receipts.
        # _ack_pending: fifo of (peer_key, entry_idx) waiting for msg_id assignment
        # _uuid_to_loc: msg_id → (peer_key, entry_idx) once assigned
        self._ack_pending: list[tuple[str, int]] = []
        self._uuid_to_loc: dict[str, tuple[str, int]] = {}
        # sender_hex → (attempt, content_id, approx_kb, timestamp) — set by fragment_completed,
        # consumed by the next file event from that sender (expires after 120 s)
        self._pending_completions: dict[str, tuple[int, str, int, float]] = {}

        self._build_ui()

    # ── Widget construction ───────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # Peer panel
        self.peer_walker = urwid.SimpleListWalker([])
        self.peer_listbox = urwid.ListBox(self.peer_walker)
        peer_title = urwid.AttrMap(urwid.Text(" Peers", align="left"), "panel_title")
        peer_panel = urwid.Pile([
            ("pack", peer_title),
            ("pack", urwid.AttrMap(urwid.Divider("─"), "divider")),  # ─
            ("weight", 1, self.peer_listbox),
        ])

        # Chat panel
        self.chat_walker = urwid.SimpleListWalker([])
        self.chat_listbox = urwid.ListBox(self.chat_walker)
        self.chat_title_w = urwid.Text(" Global", align="left")
        chat_title = urwid.AttrMap(self.chat_title_w, "panel_title")
        chat_panel = urwid.Pile([
            ("pack", chat_title),
            ("pack", urwid.AttrMap(urwid.Divider("─"), "divider")),
            ("weight", 1, self.chat_listbox),
        ])

        body = urwid.Columns([
            ("fixed", PEER_PANEL_W, peer_panel),
            ("fixed", 1, urwid.SolidFill("│")),   # │ vertical divider
            chat_panel,
        ], dividechars=0)

        self.header_text = urwid.Text(" BitChatPi  [connecting...]", align="left")
        header = urwid.AttrMap(self.header_text, "header")

        self.input_edit = urwid.Edit("> ")
        footer = urwid.AttrMap(self.input_edit, "input_bar")

        self.frame = urwid.Frame(
            body=body,
            header=header,
            footer=footer,
            focus_part="footer",
        )

        screen = urwid.raw_display.Screen()
        screen.set_terminal_properties(colors=2**24)
        self.loop = urwid.MainLoop(
            self.frame,
            PALETTE,
            screen=screen,
            handle_mouse=True,
            unhandled_input=self._handle_key,
        )
        self._wakeup_fd = self.loop.watch_pipe(self._on_wakeup)

        self._rebuild_peer_list()

    # ── Peer list ─────────────────────────────────────────────────────────────

    def _rebuild_peer_list(self) -> None:
        try:
            old_pos = self.peer_listbox.focus_position
        except IndexError:
            old_pos = 0

        items = [
            PeerButton(k, n, k == self.active, k in self.unread,
                       self._on_peer_select)
            for k, n in self.peers.items()
        ]
        self.peer_walker[:] = items

        if items:
            try:
                self.peer_listbox.focus_position = min(old_pos, len(items) - 1)
            except IndexError:
                pass
        else:
            self.peer_listbox.set_focus_pending = None

    def _on_peer_select(self, peer_id: str) -> None:
        self.active = peer_id
        self.unread.discard(peer_id)
        nick = self.peers.get(peer_id, peer_id[:8] if peer_id != GLOBAL_KEY else "Global")
        label = nick if peer_id != GLOBAL_KEY else "Global Chat"
        self.chat_title_w.set_text(f" {label}")
        self._rebuild_peer_list()
        self._rebuild_chat()
        self.frame.focus_position = "footer"

    # ── Chat area ─────────────────────────────────────────────────────────────

    def _rebuild_chat(self) -> None:
        items = [self._make_chat_row(e) for e in self.chats[self.active]]
        self.chat_walker[:] = items
        if not items:
            # urwid stores a set_focus_pending offset that becomes invalid when
            # the walker is emptied; clear it to prevent IndexError on next render
            self.chat_listbox.set_focus_pending = None
        self._scroll_chat_bottom()

    def _make_chat_row(self, entry: tuple) -> urwid.Widget:
        ts, label, text, attr = entry
        prefix = f"[{ts}] {label}: "
        if text.startswith(_FILE_TAG):
            path = text[len(_FILE_TAG):]
            return FileButton(prefix, path, on_open=self._preview_file)
        if '\033[' in text:
            return urwid.Text([("divider", prefix)] + _ansi_to_urwid(text))
        return urwid.Text([("divider", prefix), (attr, text)])

    def _preview_file(self, path: str) -> None:
        """On Enter/click: render image as ASCII art, or show the saved path."""
        if not os.path.exists(path):
            self._add_sys(self.active, f"File not found: {path}")
            self.loop.draw_screen()
            return
        import mimetypes
        mime = mimetypes.guess_type(path)[0] or ""
        if mime.startswith("image/"):
            self._render_image(self.active, path)
        else:
            self._add_sys(self.active, f"File saved: {path}")
        self.loop.draw_screen()

    def _append_to_chat(self, entry: tuple) -> None:
        """Append a single message row to the live chat walker."""
        self.chat_walker.append(self._make_chat_row(entry))
        if len(self.chat_walker) > 500:
            del self.chat_walker[0]
        self._scroll_chat_bottom()

    def _scroll_chat_bottom(self) -> None:
        if self.chat_walker:
            try:
                self.chat_listbox.focus_position = len(self.chat_walker) - 1
            except IndexError:
                pass

    # ── Message state ─────────────────────────────────────────────────────────

    def _add_msg(self, key: str, label: str, text: str, attr: str) -> None:
        ts = datetime.now().strftime("%H:%M")
        entry = (ts, label, text, attr)
        self.chats[key].append(entry)
        if len(self.chats[key]) > 500:
            self.chats[key] = self.chats[key][-500:]
        if key == self.active:
            self._append_to_chat(entry)
        else:
            self.unread.add(key)

    def _add_sys(self, key: str, text: str) -> None:
        self._add_msg(key, "***", text, "chat_system")

    def _render_image(self, key: str, path: str) -> None:
        """Render an image file as ASCII art lines into the chat."""
        try:
            import numpy as np
            from PIL import Image
            from pathlib import Path as _Path
            import sys as _sys
            client_dir = str(_Path(__file__).resolve().parent)
            if client_dir not in _sys.path:
                _sys.path.insert(0, client_dir)
            from img2ContourAscii import Renderer, CELL_ASPECT
            try:
                cols, rows = self.loop.screen.get_cols_rows()
            except Exception:
                cols, rows = 80, 24
            # chat area = cols - peer_panel(22) - divider(1)
            # prefix "[HH:MM]    : " = 13 chars
            img_width = max(20, cols - PEER_PANEL_W - 1 - 13)
            # header(1) + title(1) + divider(1) + footer(1) + sys-msg row(1) = 5
            max_img_rows = max(4, rows - 5)
            img = Image.open(path).convert("RGB")
            iw, ih = img.size
            # Reduce cols if the image would exceed max_img_rows at full width
            rows_at_full = max(1, round(ih * img_width / (iw * CELL_ASPECT)))
            if rows_at_full > max_img_rows:
                img_width = max(20, int(max_img_rows * iw * CELL_ASPECT / ih))
            renderer = Renderer(cols=img_width, autocontrast=True, use_color=True)
            art = renderer.render_frame(np.array(img, dtype=np.float32))
            for line in art.split("\n"):
                self._add_msg(key, "   ", line, "chat_other")
        except Exception as exc:
            self._add_sys(key, f"Img2ContourAscii preview failed: {exc}")

    # ── Input handling ────────────────────────────────────────────────────────

    def _handle_key(self, key: str) -> None:
        if key == "enter":
            if self.frame.focus_position == "footer":
                self._send_input()
        elif key == "tab":
            # Toggle: footer ↔ chat panel (so Enter on a file row works)
            try:
                if self.frame.focus_position == "footer":
                    self.frame.focus_position = "body"
                    self.frame.body.focus_position = 2  # chat column
                else:
                    self.frame.focus_position = "footer"
            except Exception:
                self.frame.focus_position = "footer"
        elif key in ("ctrl c", "ctrl q"):
            raise urwid.ExitMainLoop()

    def _send_input(self) -> None:
        text = self.input_edit.edit_text.strip()
        self.input_edit.edit_text = ""
        if not text:
            return

        # /file <path> — upload a file to the active DM peer
        if text.startswith("/file ") and self.active != GLOBAL_KEY:
            path = text[6:].strip()
            if not path:
                self._add_sys(self.active, "Usage: /file <path>")
                return
            if not os.path.isfile(path):
                self._add_sys(self.active, f"Not found: {path}")
                return
            self.ipc_q.put({"cmd": "send_file", "to": self.active, "path": path})
            self._add_msg(self.active, "me", f"{_FILE_TAG}{path}", "chat_self")
            idx = len(self.chats[self.active]) - 1
            self._ack_pending.append((self.active, idx))
            return

        if self.active == GLOBAL_KEY:
            self.ipc_q.put({"cmd": "broadcast", "content": text})
            self._add_msg(GLOBAL_KEY, "me", text, "chat_self")
            # broadcasts don't get receipts, no UUID tracking needed
        else:
            self.ipc_q.put({"cmd": "send", "to": self.active, "content": text})
            self._add_msg(self.active, "me", text, "chat_self")
            # record location so we can stamp a checkmark when receipt arrives
            idx = len(self.chats[self.active]) - 1
            self._ack_pending.append((self.active, idx))

    # ── IPC event processing ──────────────────────────────────────────────────

    def _on_wakeup(self, data: bytes) -> None:
        """Called in main thread by urwid watch_pipe when IPC thread has events."""
        peers_changed = False
        while True:
            try:
                kind, payload = self.ui_q.get_nowait()
            except queue.Empty:
                break
            if kind == "sys":
                self._add_sys(GLOBAL_KEY, payload)
            elif kind == "connected":
                self.connected = True
                self.header_text.set_text(" BitChatPi  [connected]")
                self.ipc_q.put({"cmd": "peers"})
            elif kind == "disconnected":
                self.connected = False
                self.header_text.set_text(" BitChatPi  [disconnected]")
            elif kind == "ipc":
                try:
                    changed = self._handle_ipc_event(payload)
                    peers_changed = peers_changed or changed
                except Exception as exc:
                    self._add_sys(GLOBAL_KEY, f"[TUI error] {exc}")

        if peers_changed:
            self._rebuild_peer_list()
        self.loop.draw_screen()

    def _handle_ipc_event(self, obj: dict) -> bool:
        """Process one IPC event. Returns True if peer list needs rebuild."""
        ev = obj.get("event")

        if ev == "peers":
            changed = False
            for p in obj.get("list", []):
                pid = p.get("peer_id", "")
                nick = p.get("nick", pid[:8])
                if pid and pid not in self.peers:
                    self.peers[pid] = nick
                    self._add_sys(GLOBAL_KEY, f"Peer online: {nick}")
                    changed = True
            return changed

        if ev == "peer":
            pid = obj.get("peer_id", "")
            nick = obj.get("nick", pid[:8] if pid else "?")
            action = obj.get("action", "")
            if action == "seen":
                new = pid not in self.peers
                self.peers[pid] = nick
                if new:
                    self._add_sys(GLOBAL_KEY, f"+ {nick}")
                return True
            if action == "lost":
                if pid in self.peers:
                    del self.peers[pid]
                    self._add_sys(GLOBAL_KEY, f"- {nick}")
                    return True
            return False

        if ev == "message":
            if obj.get("self"):
                return False  # already shown locally when user pressed Enter
            from_id = obj.get("from", "")
            nick = obj.get("nick") or (from_id[:8] if from_id else "?")
            content = obj.get("content", "")
            private = obj.get("private", False)
            if private and from_id:
                is_new = from_id not in self.peers
                if is_new:
                    self.peers[from_id] = nick
                self._add_msg(from_id, nick, content, "chat_other")
                # Rebuild if new peer, or message landed in unread (not active chat)
                return is_new or from_id != self.active
            else:
                self._add_msg(GLOBAL_KEY, nick, content, "chat_other")
                return GLOBAL_KEY != self.active

        if ev == "receipt":
            rtype = obj.get("type", "")
            ref   = obj.get("ref", "")
            sym   = "✓✓" if rtype == "read" else "✓"
            # Keep the entry in _uuid_to_loc until the read receipt arrives so
            # a delivery receipt doesn't consume the slot before read can upgrade it.
            if rtype == "read":
                loc = self._uuid_to_loc.pop(ref, None)
            else:
                loc = self._uuid_to_loc.get(ref)
            if loc:
                peer_key, idx = loc
                if 0 <= idx < len(self.chats[peer_key]):
                    ts, lbl, txt, attr = self.chats[peer_key][idx]
                    if txt.startswith(_FILE_TAG):
                        # Never mutate the file path — ticks on file rows are skipped
                        pass
                    else:
                        # Strip any tick already appended before upgrading ✓ → ✓✓
                        for suffix in ("  ✓✓", "  ✓"):
                            if txt.endswith(suffix):
                                txt = txt[: -len(suffix)]
                                break
                        self.chats[peer_key][idx] = (ts, lbl, txt + f"  {sym}", attr)
                        if peer_key == self.active and idx < len(self.chat_walker):
                            self.chat_walker[idx] = self._make_chat_row(
                                self.chats[peer_key][idx])
            # else: receipt for an auto-sent or untracked message — ignore silently
            return False

        if ev == "file":
            from_id = obj.get("from", "")
            nick    = obj.get("nick") or (from_id[:8] if from_id else "?")
            path    = obj.get("path", "")
            mime    = obj.get("mime", "")
            name    = obj.get("name", os.path.basename(path) if path else "file")
            target  = from_id if from_id else GLOBAL_KEY
            is_new  = from_id and from_id not in self.peers
            if from_id:
                self.peers[from_id] = nick
            completion = self._pending_completions.pop(from_id, None)
            if completion and time.time() - completion[3] <= 120:
                attempt, content_id, approx_kb, _ = completion
                tag = f" [#{content_id}]" if content_id else ""
                self._add_sys(target,
                    f"File received: {name}  [{mime}]  saved: {path}"
                    f"  — completes partial{tag} (attempt #{attempt})")
            else:
                self._add_sys(target, f"File received: {name}  [{mime}]  saved: {path}")
            # Clickable preview row only for images (audio/video can't be played in the TUI)
            if mime.startswith("image/"):
                self._add_msg(target, nick, f"{_FILE_TAG}{path}", "chat_system")
            return bool(is_new) or target != self.active

        if ev == "fragment_partial":
            from_id          = obj.get("from", "")
            nick             = obj.get("nick") or (from_id[:8] if from_id else "?")
            received         = obj.get("received", 0)
            total            = obj.get("total", 0)
            missing          = obj.get("missing", [])
            combined_received = obj.get("combined_received", received)
            combined_missing  = obj.get("combined_missing", missing)
            attempt          = obj.get("attempt", 1)
            content_id       = obj.get("content_id", "")
            approx_kb        = obj.get("approx_kb", 0)
            target           = from_id if from_id else GLOBAL_KEY
            if from_id and from_id not in self.peers:
                self.peers[from_id] = nick
            pct          = int(100 * received / total) if total else 0
            combined_pct = int(100 * combined_received / total) if total else 0
            name = f"~{approx_kb}KB image" if approx_kb else "image"
            if content_id:
                name += f" [#{content_id}]"
            self._add_sys(target,
                f"[PARTIAL IMAGE] Attempt #{attempt} — {name} — "
                f"this attempt: {received}/{total} ({pct}%) missing=[{_fmt_ranges(missing)}]")
            if attempt > 1:
                self._add_sys(target,
                    f"  combined across all attempts: {combined_received}/{total} "
                    f"({combined_pct}%) missing=[{_fmt_ranges(combined_missing)}]")
            if attempt == 1:
                self._add_sys(target,
                    "  Image incomplete — the sender has been notified automatically; "
                    "it will appear if they resend.")
            return target != self.active

        if ev == "fragment_set_started":
            from_id    = obj.get("from", "")
            nick       = obj.get("nick") or (from_id[:8] if from_id else "?")
            total      = obj.get("total", 0)
            attempt    = obj.get("attempt", 2)
            inherited  = obj.get("inherited", 0)
            content_id = obj.get("content_id", "")
            target     = from_id if from_id else GLOBAL_KEY
            if from_id and from_id not in self.peers:
                self.peers[from_id] = nick
            missing = total - inherited
            pct     = int(100 * inherited / total) if total else 0
            cid_tag = f" [#{content_id}]" if content_id else ""
            self._add_sys(target,
                f"[RESUMING] Attempt #{attempt}{cid_tag} — "
                f"already have {inherited}/{total} ({pct}%), need {missing} more")
            return target != self.active

        if ev == "fragment_completed":
            from_id    = obj.get("from", "")
            attempt    = obj.get("attempt", 2)
            approx_kb  = obj.get("approx_kb", 0)
            content_id = obj.get("content_id", "")
            if from_id:
                self._pending_completions[from_id] = (attempt, content_id, approx_kb, time.time())
            return False

        # {"ok": true, "msg_id": "..."} — assign UUID to pending sent message
        if obj.get("ok") and "msg_id" in obj:
            msg_id = obj["msg_id"]
            if self._ack_pending:
                self._uuid_to_loc[msg_id] = self._ack_pending.pop(0)
            return False

        if "ok" in obj and not obj["ok"]:
            self._add_sys(self.active, f"Error: {obj.get('error', '?')}")

        return False

    # ── Entry point ───────────────────────────────────────────────────────────

    def _wake(self) -> None:
        try:
            os.write(self._wakeup_fd, b"\x00")
        except OSError:
            pass

    def run(self) -> None:
        ipc_t = threading.Thread(target=_ipc_thread, args=(self,), daemon=True)
        ipc_t.start()
        try:
            self.loop.run()
        except KeyboardInterrupt:
            pass
        finally:
            self.ipc_q.put(None)
            ipc_t.join(timeout=2)


# ── IPC asyncio thread ────────────────────────────────────────────────────────

def _ipc_thread(app: App) -> None:
    asyncio.run(_ipc_run(app))


async def _ipc_run(app: App) -> None:
    def wake(kind: str, payload) -> None:
        app.ui_q.put((kind, payload))
        app._wake()

    try:
        reader, writer = await asyncio.open_unix_connection(app.sock_path)
    except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
        wake("sys", f"IPC error: {exc}")
        return

    wake("connected", None)
    done = asyncio.Event()

    async def _reader() -> None:
        while not done.is_set():
            try:
                raw = await asyncio.wait_for(reader.readline(), timeout=0.2)
            except asyncio.TimeoutError:
                continue
            except Exception:
                break
            if not raw:
                break
            try:
                wake("ipc", json.loads(raw.decode()))
            except json.JSONDecodeError:
                pass
        done.set()
        wake("disconnected", None)

    async def _sender() -> None:
        while not done.is_set():
            try:
                cmd = app.ipc_q.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.05)
                continue
            if cmd is None:
                done.set()
                break
            try:
                writer.write((json.dumps(cmd) + "\n").encode())
                await writer.drain()
            except Exception:
                done.set()
                break

    await asyncio.gather(_reader(), _sender())
    try:
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--sock", default=DEFAULT_SOCK, help="path to api.sock")
    args = ap.parse_args()
    App(args.sock).run()
    print("bye.")


if __name__ == "__main__":
    main()
