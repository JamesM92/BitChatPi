#!/usr/bin/env bash
# Run the BitChatPi daemon in the foreground (debug / dev mode).
# Stops the systemd service first so they don't compete for the BLE adapter,
# GATT registration, or the IPC socket lock.
# Restarts the systemd service automatically when this script exits.
#
# Normal:           sudo ./start-server.sh
# Full BLE reset:   sudo ./start-server.sh --reset-ble
set -e
cd "$(dirname "$0")"

LOG="${BITCHATD_LOG:-/root/.config/bitchatd/bitchatd.log}"
SOCK=/root/.config/bitchatd/api.sock
RESET_BLE=0

for arg in "$@"; do
  [ "$arg" = "--reset-ble" ] && RESET_BLE=1
done

if [ "$(id -u)" -ne 0 ]; then
  echo "Must be run as root (BlueZ requires root)."
  exit 1
fi

if systemctl is-active --quiet bitchatd 2>/dev/null; then
  echo "Stopping bitchatd systemd service..."
  systemctl stop bitchatd
  sleep 2
fi

if pgrep -f "python3.*daemon.py" > /dev/null 2>&1; then
  echo "Stopping stray daemon process(es)..."
  pkill -TERM -f "python3.*daemon.py" 2>/dev/null || true
  sleep 3
fi

if [ "$RESET_BLE" = "1" ]; then
  echo "Full BLE reset..."
  systemctl restart bluetooth
  sleep 3
fi

echo "Daemon log: $LOG"
echo "Press Ctrl-C to stop."

.venv/bin/python3 server/daemon.py --sock "$SOCK"

# Re-enable the background service when this foreground run exits.
systemctl start bitchatd 2>/dev/null || true
