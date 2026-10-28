#!/usr/bin/env bash
set -e

# If wlan0 does not exist, provision a macvlan or macvtap sub-interface over eth0
if ! ip link show dev wlan0 >/dev/null 2>&1 && ip link show dev eth0 >/dev/null 2>&1; then
    ip link add link eth0 name wlan0 type macvlan mode bridge
    ip link set dev wlan0 up
fi

exec "$@"