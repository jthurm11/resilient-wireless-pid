#!/usr/bin/env bash
# scripts/c2_ctl.sh: CLI interaction utility for DCS Command & Control API

set -Eeuo pipefail

# ANSI Palette
BL="\033[36m"
GN="\033[1;92m"
YW="\033[33m"
RD="\033[01;31m"
BOLD="\033[1m"
CL="\033[m"

msg_info()    { printf "${GN}[INFO]${CL} %s\n" "$1"; }
msg_warn()    { printf "${YW}[WARNING]${CL} %s\n" "$1"; }
msg_error()   { printf "${RD}[ERROR]${CL} %s\n" "$1" >&2; }

C2_URL="${C2_URL:-http://127.0.0.1:5050}"
ACTION="${1:-status}"
ARG="${2:-}"

command -v curl >/dev/null 2>&1 || { msg_error "Dependency 'curl' not found in PATH."; exit 1; }
command -v python3 >/dev/null 2>&1 || { msg_error "Dependency 'python3' not found in PATH."; exit 1; }

case "$ACTION" in
  status)
    msg_info "Querying runtime status from ${C2_URL}..."
    curl -sf "${C2_URL}/api/status" | python3 -m json.tool
    ;;

  sp|setpoint)
    VAL="${ARG:-50.0}"
    msg_info "Dispatching setpoint target: ${VAL}%"
    curl -sf -X POST "${C2_URL}/api/control" \
      -H "Content-Type: application/json" \
      -d "{\"is_running\": true, \"setpoint\": ${VAL}}" | python3 -m json.tool
    ;;

  algo|mode)
    MODE="${ARG:-discrete}"
    msg_info "Switching active control law: ${MODE}"
    curl -sf -X POST "${C2_URL}/api/control" \
      -H "Content-Type: application/json" \
      -d "{\"is_running\": true, \"algorithm\": \"${MODE}\"}" | python3 -m json.tool
    ;;

  start)
    TRIAL="${ARG:-trial_$(date +%s)}"
    msg_info "Initializing experiment trial: ${TRIAL}"
    curl -sf -X POST "${C2_URL}/api/control" \
      -H "Content-Type: application/json" \
      -d "{\"is_running\": true, \"trial_id\": \"${TRIAL}\"}" | python3 -m json.tool
    ;;

  stop)
    msg_warn "De-energizing plant actuation..."
    curl -sf -X POST "${C2_URL}/api/stop" | python3 -m json.tool
    ;;

  *)
    printf "${BOLD}Usage:${CL} %s {status | start [trial_id] | stop | sp [value] | mode [discrete|smith|resilient]}\n" "$0"
    exit 1
    ;;
esac