#!/usr/bin/env bash
# scripts/infra/telemetry_stack.sh
# Orchestrates InfluxDB v2 and Grafana services via Docker Compose.
# Provides centralized telemetry ingestion and dashboard visualization lifecycle control.

set -Eeuo pipefail

YW="\033[33m"
BL="\033[36m"
RD="\033[01;31m"
GN="\033[1;92m"
CL="\033[m"
BOLD="\033[1m"
TAB="  "

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/deploy/docker/telemetry-compose.yml"

SHOW_HEADER=true
ACTION=""

header_info() {
  if [ "$SHOW_HEADER" = false ]; then return; fi
  cat <<"EOF"
    ____            _ _ _            __     ____  ________ 
   / __ \___  _____(_) (_)__  ____  / /_   / __ \/  _/ __ \
  / /_/ / _ \/ ___/ / / / _ \/ __ \/ __/  / /_/ // // / / /
 / _, _/  __(__  ) / / /  __/ / / / /_   / ____// // /_/ / 
/_/ |_|\___/____/_/_/_/\___/_/ /_/\__/  /_/   /___/_____/  
                                                           
EOF
  echo -e "${BL}${BOLD}Distributed Control System - Telemetry Stack Orchestrator${CL}\n"
}

print_usage() {
  """Renders CLI usage and option descriptions to stdout."""
  cat <<EOF
Usage: $0 {create|destroy|status} [options]

Commands:
  create          Start InfluxDB v2 and Grafana containers
  destroy         Stop containers, prune network, and remove volumes
  status          Show container status and port mappings

Options:
  -h, --help      Display this help message and exit
  -q, --no-header Suppress ASCII header display
EOF
}

msg_info()  { echo -ne "${TAB}${YW}[INFO]${CL} $1..."; }
msg_ok()    { echo -e "\r\033[K${TAB}${GN}[OK]${CL} $1"; }
msg_error() { echo -e "\r\033[K${TAB}${RD}[ERROR]${CL} $1"; exit 1; }

parse_cli_arguments() {
  """
  Parses command line arguments and options.

  Args:
      $@: Raw CLI parameters.
  """
  for arg in "$@"; do
    case "$arg" in
      -h|--help)
        header_info
        print_usage
        exit 0
        ;;
      -q|--no-header)
        SHOW_HEADER=false
        ;;
      create|destroy|status)
        ACTION="$arg"
        ;;
      *)
        msg_error "Unknown argument: '$arg'. Run '$0 --help' for usage."
        ;;
    esac
  done

  if [ -z "$ACTION" ]; then
    header_info
    print_usage
    exit 1
  fi
}

check_docker() {
  """Validates Docker Engine and Compose v2 plugin availability."""
  command -v docker >/dev/null 2>&1 || msg_error "Docker is not installed."
  docker compose version >/dev/null 2>&1 || msg_error "Docker Compose v2 plugin is required."
}

create_stack() {
  """Instantiates InfluxDB v2 and Grafana containers in detached mode."""
  check_docker
  echo -e "${BOLD}Provisioning Telemetry Services via Compose...${CL}"
  msg_info "Starting InfluxDB v2 and Grafana containers"
  docker compose -f "$COMPOSE_FILE" up -d >/dev/null 2>&1
  msg_ok "Telemetry stack operational"

  echo -e "\n${GN}${BOLD}Telemetry Stack Ready.${CL}"
  echo -e "${TAB}${BOLD}InfluxDB API:${CL} http://127.0.0.1:8086 (Org: resilient_pid, Bucket: wireless_pid_metrics)"
  echo -e "${TAB}${BOLD}Grafana Web:${CL}  http://127.0.0.1:3000 (Auth: admin/admin)\n"
}

destroy_stack() {
  """Terminates containers, deletes telemetry network, and purges volumes."""
  check_docker
  echo -e "${BOLD}Tearing down Telemetry Services...${CL}"
  msg_info "Stopping containers and pruning network"
  docker compose -f "$COMPOSE_FILE" down -v >/dev/null 2>&1
  msg_ok "Telemetry stack decommissioned"
}

status_stack() {
  """Queries process status for telemetry compose services."""
  check_docker
  echo -e "${BOLD}Current Telemetry Service Status:${CL}\n"
  docker compose -f "$COMPOSE_FILE" ps
  echo ""
}

header_info
parse_cli_arguments "$@"

case "$ACTION" in
  create)  create_stack ;;
  destroy) destroy_stack ;;
  status)  status_stack ;;
esac