#!/usr/bin/env bash
# Restart the BitChatPi systemd service (bitchatd).
# Waits for the daemon socket to reappear before exiting.
#
# Usage:  sudo ./restart-service.sh
set -e

SOCK=/root/.config/bitchatd/api.sock

if [ "$(id -u)" -ne 0 ]; then
  echo "Must be run as root."
  exit 1
fi

echo "Restarting bitchatd..."
systemctl restart bitchatd

echo -n "Waiting for socket"
for i in $(seq 1 20); do
  if [ -S "$SOCK" ]; then
    echo ""
    echo "bitchatd is up."
    systemctl status bitchatd --no-pager -l
    exit 0
  fi
  echo -n "."
  sleep 1
done

echo ""
echo "Socket not found after 20s — check logs:"
echo "  journalctl -u bitchatd -n 40 --no-pager"
exit 1
