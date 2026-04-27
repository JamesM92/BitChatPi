#!/usr/bin/env bash
# Connect the BitChatPi TUI to a running daemon.
# The daemon can be the systemd service or a foreground instance started
# with start-server.sh.  No root required — the socket is world-writable.
#
# Usage:  ./start-client.sh [--sock PATH]
#
# Default socket: /root/.config/bitchatd/api.sock
# Override:       BITCHATD_SOCK=/tmp/custom.sock ./start-client.sh
cd "$(dirname "$0")"

SOCK="${BITCHATD_SOCK:-/root/.config/bitchatd/api.sock}"

for arg in "$@"; do
  case "$arg" in
    --sock) shift; SOCK="$1" ;;
    --sock=*) SOCK="${arg#--sock=}" ;;
  esac
done

if [ ! -S "$SOCK" ]; then
  echo "Daemon socket not found: $SOCK"
  echo "Start the daemon first:  sudo ./start-server.sh"
  echo "  or:                    sudo systemctl start bitchatd"
  exit 1
fi

exec .venv/bin/python3 client/tui.py --sock "$SOCK"
