#!/usr/bin/env bash
# Launch BitChatPi daemon + TUI.
# Daemon output goes to $LOG so it never pollutes the terminal.
# In a second terminal run:  tail -f /tmp/bitchatd.log
set -e
cd "$(dirname "$0")"

LOG="${BITCHATD_LOG:-/tmp/bitchatd.log}"
SOCK=/root/.config/bitchatd/api.sock

# Kill any stale daemon first so BlueZ doesn't get confused by two registrations
echo "Stopping any existing daemon..."
sudo pkill -f "python3 server/daemon.py" 2>/dev/null; sleep 1

echo "Restarting bluetooth..."
sudo systemctl restart bluetooth
sleep 2

echo "Daemon log: $LOG"
sudo bash -c "
  .venv/bin/python3 server/daemon.py >> '$LOG' 2>&1 &
  DPID=\$!
  sleep 3
  .venv/bin/python3 client/tui.py --sock '$SOCK'
  kill \$DPID 2>/dev/null
"
