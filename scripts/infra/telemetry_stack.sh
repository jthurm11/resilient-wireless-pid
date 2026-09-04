#!/usr/bin/env bash

# ------------------------------------------------------------------------------
# Script: scripts/infra/telemetry_stack.sh
# Purpose: Orchestrate InfluxDB v2 and Grafana containers for DCS telemetry
# Usage: ./telemetry_stack.sh [create|destroy|status]
# ------------------------------------------------------------------------------

set -Eeuo pipefail

# Style & Formatting (Proxmox Helper Scripts Theme)
YW=$(echo "\033[33m")
BL=$(echo "\033[36m")
RD=$(echo "\033[01;31m")
GN=$(echo "\033[1;92m")
CL=$(echo "\033[m")
BOLD=$(echo "\033[1m")
TAB="  "

NETWORK_NAME="pid-telemetry-net"
INFLUX_CONTAINER="influxdb"
GRAFANA_CONTAINER="grafana"

header_info() {
  cat <<"EOF"
    ____            _ _ _            __     ____  ________ 
   / __ \___  _____(_) (_)__  ____  / /_   / __ \/  _/ __ \
  / /_/ / _ \/ ___/ / / / _ \/ __ \/ __/  / /_/ // // / / /
 / _, _/  __(__  ) / / /  __/ / / / /_   / ____// // /_/ / 
/_/ |_|\___/____/_/_/_/\___/_/ /_/\__/  /_/   /___/_____/  
                                                           
EOF
  echo -e "${BL}${BOLD}DCS Telemetry Stack Orchestrator (InfluxDB v2 + Grafana)${CL}\n"
}

msg_info()  { local msg="$1"; echo -ne "${TAB}${YW}[INFO]${CL} ${msg}..."; }
msg_ok()    { local msg="$1"; echo -e "\r\033[K${TAB}${GN}[OK]${CL} ${msg}"; }
msg_error() { local msg="$1"; echo -e "\r\033[K${TAB}${RD}[ERROR]${CL} ${msg}"; exit 1; }

check_docker() {
  if ! command -v docker &>/dev/null; then
    msg_error "Docker CE daemon not detected. Ensure Docker is installed and running."
  fi
}

destroy_stack() {
  echo -e "${BOLD}Tearing down Telemetry Stack Containers...${CL}"
  
  for container in "$GRAFANA_CONTAINER" "$INFLUX_CONTAINER"; do
    if docker ps -a --format '{{.Names}}' | grep -Eq "^${container}\$"; then
      msg_info "Stopping and removing container: ${container}"
      docker rm -f "$container" >/dev/null 2>&1
      msg_ok "Removed container: ${container}"
    fi
  done

  if docker network ls --format '{{.Name}}' | grep -Eq "^${NETWORK_NAME}\$"; then
    msg_info "Removing bridge network: ${NETWORK_NAME}"
    docker network rm "$NETWORK_NAME" >/dev/null 2>&1
    msg_ok "Purged bridge network: ${NETWORK_NAME}"
  fi

  echo -e "\n${GN}${BOLD}Telemetry infrastructure completely decommissioned.${CL}\n"
}

create_stack() {
  echo -e "${BOLD}Provisioning Telemetry Stack Services...${CL}"

  # Idempotent pre-cleanup
  for container in "$GRAFANA_CONTAINER" "$INFLUX_CONTAINER"; do
    if docker ps -a --format '{{.Names}}' | grep -Eq "^${container}\$"; then
      msg_info "Stale container ${container} detected, purging"
      docker rm -f "$container" >/dev/null 2>&1
      msg_ok "Purged stale container: ${container}"
    fi
  done

  # Network attachment
  if ! docker network ls --format '{{.Name}}' | grep -Eq "^${NETWORK_NAME}\$"; then
    msg_info "Creating bridge network: ${NETWORK_NAME}"
    docker network create "$NETWORK_NAME" >/dev/null 2>&1
    msg_ok "Created isolated network: ${NETWORK_NAME}"
  else
    msg_ok "Network ${NETWORK_NAME} already initialized"
  fi

  # InfluxDB v2 Service
  msg_info "Starting InfluxDB v2 engine (Port 8086)"
  docker run -d \
    --name "$INFLUX_CONTAINER" \
    --network "$NETWORK_NAME" \
    --restart unless-stopped \
    -p 8086:8086 \
    -e DOCKER_INFLUXDB_INIT_MODE=setup \
    -e DOCKER_INFLUXDB_INIT_USERNAME=admin \
    -e DOCKER_INFLUXDB_INIT_PASSWORD=adminpassword \
    -e DOCKER_INFLUXDB_INIT_ORG=resilient_pid \
    -e DOCKER_INFLUXDB_INIT_BUCKET=wireless_pid_metrics \
    -e DOCKER_INFLUXDB_INIT_ADMIN_TOKEN=testbed_secret_token_123 \
    influxdb:2.7 >/dev/null 2>&1
  msg_ok "InfluxDB v2 online (http://localhost:8086)"

  # Grafana OSS Service
  msg_info "Starting Grafana visualization server (Port 3000)"
  docker run -d \
    --name "$GRAFANA_CONTAINER" \
    --network "$NETWORK_NAME" \
    --restart unless-stopped \
    -p 3000:3000 \
    grafana/grafana-oss:latest >/dev/null 2>&1
  msg_ok "Grafana UI online (http://localhost:3000)"

  echo -e "\n${GN}${BOLD}Telemetry Stack Ready.${CL}"
  echo -e "${TAB}${BOLD}InfluxDB API:${CL} http://localhost:8086 (Org: resilient_pid, Bucket: wireless_pid_metrics)"
  echo -e "${TAB}${BOLD}Grafana Web:${CL}  http://localhost:3000 (Auth: admin/admin)\n"
}

status_stack() {
  echo -e "${BOLD}Current Telemetry Service Status:${CL}\n"
  docker ps --filter "name=${INFLUX_CONTAINER}|${GRAFANA_CONTAINER}" \
    --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
  echo ""
}

# Entrypoint Execution
header_info
check_docker

case "${1:-}" in
  create)
    create_stack
    ;;
  destroy)
    destroy_stack
    ;;
  status)
    status_stack
    ;;
  *)
    echo -e "${RD}Usage:${CL} $0 {create|destroy|status}\n"
    exit 1
    ;;
esac