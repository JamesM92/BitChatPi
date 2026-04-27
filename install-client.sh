#!/usr/bin/env bash
# Install the BitChatPi TUI client.
# Copies client files into the existing server install and adds the
# Python packages required for the TUI (urwid) and image preview (pillow).
#
# Usage:  sudo ./install-client.sh
# Re-run at any time to update.
set -e
cd "$(dirname "$0")"

INSTALL_DIR=/opt/bitchatpi
VENV=$INSTALL_DIR/.venv

if [ "$(id -u)" -ne 0 ]; then
  echo "Must be run as root."
  exit 1
fi

if [ ! -d "$VENV" ]; then
  echo "Server venv not found at $VENV."
  echo "Run install-server.sh first."
  exit 1
fi

# ── Copy files ─────────────────────────────────────────────────────────────────
echo "[1/2] Copying client files to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR/client"
cp -r client/. "$INSTALL_DIR/client/"
cp start-client.sh "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR/start-client.sh"

# ── Python deps ────────────────────────────────────────────────────────────────
echo "[2/2] Installing client Python packages..."
"$VENV/bin/pip" install \
  "urwid==4.0.0" \
  "pillow==12.2.0"

echo ""
echo "Done. Start the TUI with:"
echo "  $INSTALL_DIR/start-client.sh"
echo "  (no sudo required — the daemon socket is world-writable)"
