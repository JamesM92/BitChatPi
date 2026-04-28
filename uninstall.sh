#!/usr/bin/env bash
# Completely remove the BitChatPi daemon, service, and all installed files.
# Usage:  sudo ./uninstall.sh
set -e

if [ "$(id -u)" -ne 0 ]; then
  echo "Must be run as root."
  exit 1
fi

echo "[1/5] Stopping and disabling bitchatd service..."
systemctl stop bitchatd   2>/dev/null && echo "  stopped" || echo "  already stopped"
systemctl disable bitchatd 2>/dev/null && echo "  disabled" || echo "  already disabled"

echo "[2/5] Removing systemd unit file..."
rm -f /etc/systemd/system/bitchatd.service
systemctl daemon-reload
echo "  done"

echo "[3/5] Removing installed files at /opt/bitchatpi..."
rm -rf /opt/bitchatpi
echo "  done"

echo "[4/5] Removing config, logs, and identity at /root/.config/bitchatd..."
rm -rf /root/.config/bitchatd
echo "  done"

echo "[5/5] Removing received files at /var/lib/bitchatd..."
rm -rf /var/lib/bitchatd
echo "  done"

echo ""
echo "BitChatPi fully uninstalled."
