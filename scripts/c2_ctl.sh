#!/usr/bin/env bash
# scripts/c2_ctl.sh: CLI interaction utility for DCS Command & Control API

set -eo pipefail

C2_URL="${C2_URL:-http://127.0.0.1:5050}"
ACTION="${1:-status}"
ARG="${2:-}"

case "$ACTION" in
  status)
    curl -s "${C2_URL}/api/status" | python3 -m json.tool
    ;;

  sp|setpoint)
    VAL="${ARG:-50.0}"
    curl -s -X POST "${C2_URL}/api/control" \
      -H "Content-Type: application/json" \
      -d "{\"is_running\": true, \"setpoint\": ${VAL}}" | python3 -m json.tool
    ;;

  algo|mode)
    MODE="${ARG:-baseline}"
    curl -s -X POST "${C2_URL}/api/control" \
      -H "Content-Type: application/json" \
      -d "{\"is_running\": true, \"algorithm\": \"${MODE}\"}" | python3 -m json.tool
    ;;

  start)
    TRIAL="${ARG:-trial_$(date +%s)}"
    curl -s -X POST "${C2_URL}/api/control" \
      -H "Content-Type: application/json" \
      -d "{\"is_running\": true, \"trial_id\": \"${TRIAL}\"}" | python3 -m json.tool
    ;;

  stop)
    curl -s -X POST "${C2_URL}/api/stop" | python3 -m json.tool
    ;;

  *)
    echo "Usage: $0 {status | start [trial_id] | stop | sp [value] | mode [baseline|smith|resilient]}"
    exit 1
    ;;
esac