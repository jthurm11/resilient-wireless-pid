#!/usr/bin/env bash

# ------------------------------------------------------------------------------
# Script: scripts/infra/mock_environment.sh
# Purpose: Unified Orchestrator for Distributed Control Mock Environments
# Usage: ./mock_environment.sh [docker|lxc] [create|destroy|status] [--no-header]
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
COMPOSE_FILE="${REPO_ROOT}/deploy/docker/docker-compose.yml"

SHOW_HEADER=true
TARGET=""
ACTION=""

for arg in "$@"; do
  case "$arg" in
    --no-header|-q)
      SHOW_HEADER=false
      ;;
    docker|lxc)
      TARGET="$arg"
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
  echo -e "${BL}${BOLD}Distributed Control System - Mock Testbed Manager${CL}\n"
}

msg_info()  { echo -ne "${TAB}${YW}[INFO]${CL} $1..."; }
msg_ok()    { echo -e "\r\033[K${TAB}${GN}[OK]${CL} $1"; }
msg_error() { echo -e "\r\033[K${TAB}${RD}[ERROR]${CL} $1"; exit 1; }

auto_detect_target() {
  local has_pve=false
  local has_docker=false

  if [[ "$(id -u)" -eq 0 ]] && command -v pct >/dev/null 2>&1 && command -v pvesm >/dev/null 2>&1; then
    has_pve=true
  fi

  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    has_docker=true
  fi

  if [ "$has_pve" = true ] && [ "$has_docker" = false ]; then
    TARGET="lxc"
  elif [ "$has_pve" = false ] && [ "$has_docker" = true ]; then
    TARGET="docker"
  elif [ "$has_pve" = true ] && [ "$has_docker" = true ]; then
    if [ -t 0 ]; then
      echo -e "${YW}${BOLD}Ambiguous Environment:${CL} Both Proxmox VE and Docker subsystems detected."
      read -rp "Select target environment [lxc/docker]: " choice
      case "$choice" in
        lxc|docker) TARGET="$choice" ;;
        *) msg_error "Invalid selection. Aborting." ;;
      esac
    else
      msg_error "Both Proxmox and Docker detected in non-interactive shell. Specify target explicitly."
    fi
  else
    if [ -t 0 ]; then
      echo -e "${YW}${BOLD}Target Unknown:${CL} Neither Proxmox nor active Docker could be verified."
      read -rp "Manually select backend [lxc/docker]: " choice
      case "$choice" in
        lxc|docker) TARGET="$choice" ;;
        *) msg_error "Invalid selection. Aborting." ;;
      esac
    else
      msg_error "No supported backend (Proxmox VE or Docker) detected. Aborting."
    fi
  fi
}

# ==============================================================================
# DOCKER TRACK
# ==============================================================================
create_docker() {
  echo -e "${BOLD}Provisioning Docker Mock Pods & Network Topology...${CL}"
  msg_info "Building container images and initializing dcs_net bridge"
  docker compose -f "$COMPOSE_FILE" up -d --build >/dev/null 2>&1
  msg_ok "All Docker nodes online"

  echo -e "\n${GN}${BOLD}Docker Mock Testbed Ready.${CL}"
  echo -e "${TAB}${BOLD}dcs-ctrl-node: ${CL} 10.10.10.1 (UI: http://localhost:5050 | Grafana: http://localhost:3000)"
  echo -e "${TAB}${BOLD}dcs-plant-node:${CL} 10.10.10.2 (UDP Socket: 5005)\n"
}

destroy_docker() {
  echo -e "${BOLD}Tearing down Docker Mock Stack...${CL}"
  msg_info "Stopping containers and deleting virtual interfaces"
  docker compose -f "$COMPOSE_FILE" down -v >/dev/null 2>&1
  msg_ok "Docker testbed destroyed"
}

status_docker() {
  echo -e "${BOLD}Docker Infrastructure Status:${CL}\n"
  docker compose -f "$COMPOSE_FILE" ps
}

# ==============================================================================
# PROXMOX LXC TRACK (Inlined Native Provisioner)
# ==============================================================================
pve_check_storage() {
  STORAGE="local-lvm"
  if ! pvesm status -storage "$STORAGE" &>/dev/null; then
    STORAGE="local"
  fi
}

pve_configure_kernel() {
  msg_info "Configuring host kernel modules for traffic control"
  local modules=("sch_netem" "cls_u32" "ifb" "sch_tbf" "sch_prio")
  for mod in "${modules[@]}"; do
    modprobe "$mod" 2>/dev/null || true
    if ! grep -q "^${mod}$" /etc/modules-load.d/modules.conf 2>/dev/null; then
      echo "$mod" >> /etc/modules-load.d/modules.conf
    fi
  done
  msg_ok "Kernel modules active and persistent"
}

pve_configure_network() {
  msg_info "Verifying isolated DCS bridge (vmbr1)"
  if ! grep -q "iface vmbr1" /etc/network/interfaces; then
    cat <<EOF >> /etc/network/interfaces

auto vmbr1
iface vmbr1 inet manual
        bridge-ports none
        bridge-stp off
        bridge-fd 0
# Internal testing bridge for DCS traffic
EOF
    if command -v ifreload >/dev/null 2>&1; then
      ifreload -a
    else
      systemctl restart networking
    fi
    msg_ok "Created bridge vmbr1"
  else
    msg_ok "Bridge vmbr1 present"
  fi
}

pve_fetch_template() {
  msg_info "Verifying Debian 13 template"
  pveam update >/dev/null 2>&1
  TEMPLATE=$(pveam available -section system | awk '{print $2}' | grep "debian-13-standard" | head -n 1 || true)
  if [ -z "$TEMPLATE" ]; then
    TEMPLATE=$(pveam available -section system | awk '{print $2}' | grep "debian" | sort -V | tail -n 1)
  fi
  if [ ! -f "/var/lib/vz/template/cache/${TEMPLATE}" ]; then
    pveam download local "$TEMPLATE" >/dev/null 2>&1
  fi
  msg_ok "Appliance template ready: ${TEMPLATE}"
}

pve_create_container() {
  local ctid="$1"
  local hostname="$2"
  local internal_ip="$3"

  if pct status "$ctid" &>/dev/null; then
    msg_info "Destroying existing instance of CT ${ctid}"
    pct stop "$ctid" &>/dev/null || true
    pct destroy "$ctid" &>/dev/null
    msg_ok "Removed prior CT ${ctid}"
  fi

  msg_info "Instantiating CT ${ctid} (${hostname})"
  pct create "$ctid" "local:vztmpl/${TEMPLATE}" \
    --ostype debian \
    --hostname "$hostname" \
    --cores 2 \
    --memory 2048 \
    --swap 512 \
    --features nesting=1,keyctl=1 \
    --net0 name=eth0,bridge=vmbr0,ip=dhcp \
    --net1 name=wlan0,bridge=vmbr1,ip="${internal_ip}/24" \
    --storage "$STORAGE" \
    --rootfs volume="${STORAGE}:8" \
    --unprivileged 0 >/dev/null 2>&1

  pct start "$ctid" >/dev/null 2>&1
  msg_ok "Container CT ${ctid} (${hostname}) started"
}

pve_install_base_pkgs() {
  local ctid="$1"
  msg_info "Installing POSIX utilities & Python on CT ${ctid}"
  sleep 2
  pct exec "$ctid" -- bash -c "export DEBIAN_FRONTEND=noninteractive && \
    apt-get update -y >/dev/null && \
    apt-get install -y --no-install-recommends \
      iproute2 iperf3 iputils-ping python3 python3-pip python3-venv \
      build-essential git curl ca-certificates gnupg >/dev/null 2>&1"
  msg_ok "Toolchains ready on CT ${ctid}"
}

pve_install_docker() {
  local ctid="$1"
  msg_info "Configuring Docker Engine inside CT ${ctid}"
  pct exec "$ctid" -- bash -c "export DEBIAN_FRONTEND=noninteractive && \
    install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg --yes >/dev/null 2>&1 && \
    chmod a+r /etc/apt/keyrings/docker.gpg && \
    echo \"deb [arch=\$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian trixie stable\" > /etc/apt/sources.list.d/docker.list && \
    apt-get update -y >/dev/null && \
    apt-get install -y --no-install-recommends \
      docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null 2>&1 && \
    systemctl enable --now docker >/dev/null 2>&1"
  msg_ok "Docker active inside CT ${ctid}"
}

create_lxc() {
  echo -e "${BOLD}Deploying Native Proxmox LXC Infrastructure...${CL}"
  pve_check_storage
  pve_configure_kernel
  pve_configure_network
  pve_fetch_template

  # Node 1: dcs-ctrl-node
  pve_create_container 201 "dcs-ctrl-node" "10.10.10.1"
  pve_install_base_pkgs 201
  pve_install_docker 201

  # Node 2: dcs-plant-node
  pve_create_container 202 "dcs-plant-node" "10.10.10.2"
  pve_install_base_pkgs 202

  msg_info "Configuring application workspace on dcs-ctrl-node (CT 201)"
  pct exec 201 -- bash -c "
    echo DCS_ENV=proxmox_lxc >> /etc/environment
    if [ ! -d /opt/resilient-wireless-pid ]; then
      git clone https://github.com/jthurm11/resilient-wireless-pid.git /opt/resilient-wireless-pid >/dev/null 2>&1
    fi
    cd /opt/resilient-wireless-pid
    python3 -m venv .venv
    source .venv/bin/activate
    pip install --upgrade pip >/dev/null 2>&1
    pip install -r requirements.txt >/dev/null 2>&1
    pip install -e . >/dev/null 2>&1
    ./scripts/infra/telemetry_stack.sh create --no-header >/dev/null 2>&1
  "
  msg_ok "Workspace and telemetry running on dcs-ctrl-node"

  msg_info "Configuring application workspace on dcs-plant-node (CT 202)"
  pct exec 202 -- bash -c "
    echo DCS_ENV=proxmox_lxc >> /etc/environment
    if [ ! -d /opt/resilient-wireless-pid ]; then
      git clone https://github.com/jthurm11/resilient-wireless-pid.git /opt/resilient-wireless-pid >/dev/null 2>&1
    fi
    cd /opt/resilient-wireless-pid
    python3 -m venv .venv
    source .venv/bin/activate
    pip install --upgrade pip >/dev/null 2>&1
    pip install -r requirements.txt >/dev/null 2>&1
    pip install -e . >/dev/null 2>&1
  "
  msg_ok "Workspace ready on dcs-plant-node"

  echo -e "\n${GN}${BOLD}Proxmox Testbed Fully Provisioned.${CL}"
  echo -e "${TAB}${BOLD}dcs-ctrl-node: ${CL} CT 201 | wlan0: 10.10.10.1/24"
  echo -e "${TAB}${BOLD}dcs-plant-node:${CL} CT 202 | wlan0: 10.10.10.2/24\n"
}

destroy_lxc() {
  echo -e "${BOLD}Decommissioning Proxmox LXC Testbed...${CL}"
  for ctid in 201 202; do
    if pct status "$ctid" >/dev/null 2>&1; then
      msg_info "Purging container CT ${ctid}"
      pct stop "$ctid" >/dev/null 2>&1 || true
      pct destroy "$ctid" >/dev/null 2>&1
      msg_ok "Destroyed CT ${ctid}"
    fi
  done
  echo -e "\n${GN}${BOLD}LXC testbed completely destroyed.${CL}\n"
}

status_lxc() {
  echo -e "${BOLD}Proxmox LXC Testbed Status:${CL}\n"
  pct list | grep -E "201|202" || echo "No active mock containers found."
}

# ==============================================================================
# MAIN ROUTING
# ==============================================================================
header_info

if [ -z "$ACTION" ]; then
  echo -e "${RD}Missing action.${CL} Usage: $0 [docker|lxc] {create|destroy|status} [--no-header]\n"
  exit 1
fi

if [ -z "$TARGET" ]; then
  auto_detect_target
fi

case "${TARGET}:${ACTION}" in
  docker:create)  create_docker ;;
  docker:destroy) destroy_docker ;;
  docker:status)  status_docker ;;
  lxc:create)     create_lxc ;;
  lxc:destroy)    destroy_lxc ;;
  lxc:status)     status_lxc ;;
  *)
    echo -e "${RD}Invalid command combination:${CL} ${TARGET}:${ACTION}"
    exit 1
    ;;
esac