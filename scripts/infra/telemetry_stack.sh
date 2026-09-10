#!/usr/bin/env bash

# ------------------------------------------------------------------------------
# Script: scripts/infra/telemetry_stack.sh
# Purpose: Orchestrate InfluxDB v2 and Grafana via Compose
# Usage: ./telemetry_stack.sh [create|destroy|status] [--no-header]
# ------------------------------------------------------------------------------

set -Eeuo pipefail

YW=$(echo "\033[33m")
BL=$(echo "\033[36m")
RD=$(echo "\033[01;31m")
GN=$(echo "\033[1;92m")
CL=$(echo "\033[m")
BOLD=$(echo "\033[1m")
TAB="  "

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/deploy/docker/telemetry-compose.yml"

SHOW_HEADER=true
ACTION=""

for arg in "$@"; do
  case "$arg" in
    --no-header|-q)
      SHOW_HEADER=false
      ;;
    create|destroy|status)
      ACTION="$arg"
      ;;
    *)
      echo -e "${RD}Unknown argument:${CL} $arg"
      exit 1
      ;;
  esac
done

header_info() {
  if [ "$SHOW_HEADER" = false ]; then return; fi
  cat <<"EOF"
    ____            _ _ _            __     ____  ________ 
   / __ \___  _____(_) (_)__  ____  / /_   / __ \/  _/ __ \
  / /_/ / _ \/ ___/ / / / _ \/ __ \/ __/  / /_/ // // / / /
 / _, _/  __(__  ) / / /  __/ / / / /_   / ____// // /_/ / 
/_/ |_|\___/____/_/_/_/\___/_/ /_/\__/  /_/   /___/_____/  
                                                           
EOF
  echo -e "${BL}${BOLD}DCS Telemetry Stack Orchestrator (InfluxDB v2 + Grafana)${CL}\n"
}

msg_info()  { echo -ne "${TAB}${YW}[INFO]${CL} $1..."; }
msg_ok()    { echo -e "\r\033[K${TAB}${GN}[OK]${CL} $1"; }
msg_error() { echo -e "\r\033[K${TAB}${RD}[ERROR]${CL} $1"; exit 1; }

check_docker() {
  command -v docker >/dev/null 2>&1 || msg_error "Docker is not installed."
  docker compose version >/dev/null 2>&1 || msg_error "Docker Compose v2 plugin is required."
}

create_stack() {
  check_docker
  echo -e "${BOLD}Provisioning Telemetry Services via Compose...${CL}"
  msg_info "Starting InfluxDB v2 and Grafana containers"
  docker compose -f "$COMPOSE_FILE" up -d >/dev/null 2>&1
  msg_ok "Telemetry stack operational"

  echo -e "\n${GN}${BOLD}Telemetry Stack Ready.${CL}"
  echo -e "${TAB}${BOLD}InfluxDB API:${CL} http://localhost:8086 (Org: resilient_pid, Bucket: wireless_pid_metrics)"
  echo -e "${TAB}${BOLD}Grafana Web:${CL}  http://localhost:3000 (Auth: admin/admin)\n"
}

destroy_stack() {
  check_docker
  echo -e "${BOLD}Tearing down Telemetry Services...${CL}"
  msg_info "Stopping containers and pruning network"
  docker compose -f "$COMPOSE_FILE" down -v >/dev/null 2>&1
  msg_ok "Telemetry stack decommissioned"
}

status_stack() {
  check_docker
  echo -e "${BOLD}Current Telemetry Service Status:${CL}\n"
  docker compose -f "$COMPOSE_FILE" ps
  echo ""
}

header_info

case "$ACTION" in
  create)  create_stack ;;
  destroy) destroy_stack ;;
  status)  status_stack ;;
  *)
    echo -e "${RD}Usage:${CL} $0 {create|destroy|status} [--no-header]\n"
    exit 1
    ;;
esac