#!/usr/bin/env bash
ACTION="${1:-status}"
ARG="${2:-}"
URL="http://127.0.0.1:5050/api"

case "$ACTION" in
  start)
    TRIAL="${ARG:-trial_$(date +%s)}"
    curl -s -X POST "${URL}/start" -H "Content-Type: application/json" -d "{\"trial_id\": \"$TRIAL\"}"
    ;;
  stop)
    curl -s -X POST "${URL}/stop"
    ;;
  sp)
    if [ -z "$ARG" ]; then echo "Usage: $0 sp <target_value>"; exit 1; fi
    curl -s -X POST "${URL}/control" -H "Content-Type: application/json" -d "{\"setpoint\": $ARG}"
    ;;
  mode)
    if [ -z "$ARG" ]; then echo "Usage: $0 mode <baseline|smith|resilient>"; exit 1; fi
    curl -s -X POST "${URL}/control" -H "Content-Type: application/json" -d "{\"algorithm\": \"$ARG\"}"
    ;;
  status)
    curl -s -X GET "${URL}/status"
    ;;
  *)
    echo "Commands: start [trial_id], stop, sp <value>, mode <name>, status"
    ;;
esac