#!/usr/bin/env bash
# Install the BitChatPi daemon to /opt/bitchatpi and register it as a
# systemd service that starts automatically on boot.
#
# Usage:  sudo ./install-server.sh
# Re-run at any time to update an existing install.
set -e
cd "$(dirname "$0")"

INSTALL_DIR=/opt/bitchatpi
VENV=$INSTALL_DIR/.venv
UNIT_SRC=systemd/bitchatd.service
UNIT_DST=/etc/systemd/system/bitchatd.service

if [ "$(id -u)" -ne 0 ]; then
  echo "Must be run as root."
  exit 1
fi

# ── System packages ────────────────────────────────────────────────────────────
echo "[1/5] Installing system packages..."
apt-get install -y --no-install-recommends \
  python3 python3-venv python3-dev \
  bluez bluetooth \
  libglib2.0-dev

# ── Copy files ─────────────────────────────────────────────────────────────────
echo "[2/5] Copying server files to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
cp -r server "$INSTALL_DIR/"
cp start-server.sh "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR/start-server.sh"

# ── Python venv + deps ─────────────────────────────────────────────────────────
echo "[3/5] Setting up Python venv..."
if [ ! -d "$VENV" ]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install \
  "bleak==3.0.1" \
  "dbus-fast==4.0.4" \
  "dissononce==0.34.3" \
  "cryptography==47.0.0"

# ── Systemd service ────────────────────────────────────────────────────────────
echo "[4/5] Installing systemd service..."
cp "$UNIT_SRC" "$UNIT_DST"
systemctl daemon-reload
systemctl enable bitchatd

# ── Start ──────────────────────────────────────────────────────────────────────
echo "[5/5] Starting bitchatd..."
systemctl restart bitchatd

echo ""
echo "Done. Service status:"
systemctl status bitchatd --no-pager -l
echo ""
echo "Logs:    sudo journalctl -u bitchatd -f"
echo "         tail -f /root/.config/bitchatd/bitchatd.log"
