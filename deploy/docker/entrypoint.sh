#!/usr/bin/env bash
set -e

# Rename default container bridge interface eth0 to wlan0 if present
if ip link show dev eth0 >/dev/null 2>&1; then
    ip link set dev eth0 down
    ip link set dev eth0 name wlan0
    ip link set dev wlan0 up
fi

# Execute CMD passed from compose
exec "$@"