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

# Validates standard dotted-quad IPv4 format
validate_ipv4() {
  local ip="$1"
  local regex='^((25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.){3}(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])$'
  if [[ ! "$ip" =~ $regex ]]; then
    msg_error "Invalid IPv4 address format: '$ip'. Must be valid A.B.C.D notation."
    exit 1
  fi
}

usage() {
  printf "${BOLD}Usage:${CL} %s [-t <ipv4_address>] {status | start [trial_id] | stop | sp [value] | mode [discrete|smith|resilient]}\n" "$0"
  printf "  -t, --target <IP>   IPv4 address of remote ctrl node (defaults to 127.0.0.1 or \$C2_URL)\n"
  printf "                      Port 5050 and http:// scheme are wrapped automatically.\n"
  exit 1
}

# Resolve target URL
TARGET_IP=""
POSITIONAL_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    -t|--target)
      if [[ -z "${2:-}" ]]; then
        msg_error "Option '$1' requires an IPv4 address argument."
        usage
      fi
      TARGET_IP="$2"
      shift 2
      ;;
    -h|--help)
      usage
      ;;
    *)
      POSITIONAL_ARGS+=("$1")
      shift
      ;;
  esac
done

# Restore positional parameters
set -- "${POSITIONAL_ARGS[@]}"

if [[ -n "$TARGET_IP" ]]; then
  validate_ipv4 "$TARGET_IP"
  C2_URL="http://${TARGET_IP}:5050"
else
  C2_URL="${C2_URL:-http://127.0.0.1:5050}"
fi

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
    msg_info "Dispatching setpoint target: ${VAL}% to ${C2_URL}"
    curl -sf -X POST "${C2_URL}/api/control" \
      -H "Content-Type: application/json" \
      -d "{\"is_running\": true, \"setpoint\": ${VAL}}" | python3 -m json.tool
    ;;

  algo|mode)
    MODE="${ARG:-discrete}"
    msg_info "Switching active control law: ${MODE} on ${C2_URL}"
    curl -sf -X POST "${C2_URL}/api/control" \
      -H "Content-Type: application/json" \
      -d "{\"is_running\": true, \"algorithm\": \"${MODE}\"}" | python3 -m json.tool
    ;;

  start)
    TRIAL="${ARG:-trial_$(date +%s)}"
    msg_info "Initializing experiment trial: ${TRIAL} on ${C2_URL}"
    curl -sf -X POST "${C2_URL}/api/control" \
      -H "Content-Type: application/json" \
      -d "{\"is_running\": true, \"trial_id\": \"${TRIAL}\"}" | python3 -m json.tool
    ;;

  stop)
    msg_warn "De-energizing plant actuation on ${C2_URL}..."
    curl -sf -X POST "${C2_URL}/api/stop" | python3 -m json.tool
    ;;

  *)
    usage
    ;;
esac