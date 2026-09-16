#!/usr/bin/env bash
set -e

# Keep eth0 intact for Docker proxying, and alias/link wlan0 for netem testing if required
if ! ip link show dev wlan0 >/dev/null 2>&1; then
    # Add dummy or macvlan device if strict wlan0 naming is required
    ip link add wlan0 type dummy 2>/dev/null || true
fi

# Execute CMD passed from compose
exec "$@"