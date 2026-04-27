#!/usr/bin/env bash
# Launch BitChatPi daemon + TUI.
# Daemon output goes to $LOG so it never pollutes the terminal.
# In a second terminal run:  tail -f /tmp/bitchatd.log
#
# Normal restart (no phone disruption):
#   sudo ./start.sh
#
# Full BLE reset (use when GATT is stuck or after a crash):
#   sudo ./start.sh --reset-ble
set -e
cd "$(dirname "$0")"

LOG="${BITCHATD_LOG:-/tmp/bitchatd.log}"
SOCK=/root/.config/bitchatd/api.sock
RESET_BLE=0

for arg in "$@"; do
  [ "$arg" = "--reset-ble" ] && RESET_BLE=1
done

# Send SIGTERM to any running daemon and wait for it to clean up.
# A clean shutdown unregisters the GATT server and advertisement so
# BlueZ doesn't get confused — no bluetooth restart needed.
if pgrep -f "python3 server/daemon.py" > /dev/null 2>&1; then
  echo "Stopping existing daemon (SIGTERM)..."
  sudo pkill -TERM -f "python3 server/daemon.py" 2>/dev/null || true
  sleep 5   # give BlueZ time to process the unregistration
fi

if [ "$RESET_BLE" = "1" ]; then
  echo "Full BLE reset..."
  sudo systemctl restart bluetooth
  sleep 3
fi

echo "Daemon log: $LOG"
sudo bash -c "
  .venv/bin/python3 server/daemon.py >> '$LOG' 2>&1 &
  DPID=\$!
  sleep 3
  .venv/bin/python3 client/tui.py --sock '$SOCK'
  kill \$DPID 2>/dev/null
"
