#!/usr/bin/env bash

# -----------------------------------------------------------------------------
# Script: scripts/infra/proxmox_mock_provisioner.sh
# Target OS: Debian 13 (Trixie)
# Nodes: CT 201 (Controller + Telemetry Stack), CT 202 (Plant Node)
# -----------------------------------------------------------------------------

set -Eeuo pipefail

# Style & Formatting
YW=$(echo "\033[33m")
BL=$(echo "\033[36m")
RD=$(echo "\033[01;31m")
GN=$(echo "\033[1;92m")
CL=$(echo "\033[m")
BOLD=$(echo "\033[1m")
TAB="  "

header_info() {
  clear
  cat <<"EOF"
    ____            _ _ _            __     ____  ________ 
   / __ \___  _____(_) (_)__  ____  / /_   / __ \/  _/ __ \
  / /_/ / _ \/ ___/ / / / _ \/ __ \/ __/  / /_/ // // / / /
 / _, _/  __(__  ) / / /  __/ / / / /_   / ____// // /_/ / 
/_/ |_|\___/____/_/_/_/\___/_/ /_/\__/  /_/   /___/_____/  
                                                           
EOF
  echo -e "${BL}${BOLD}Distributed Control System - Mock Testbed Provisioner${CL}\n"
}

msg_info()  { local msg="$1"; echo -ne "${TAB}${YW}[INFO]${CL} ${msg}..."; }
msg_ok()    { local msg="$1"; echo -e "\r\033[K${TAB}${GN}[OK]${CL} ${msg}"; }
msg_error() { local msg="$1"; echo -e "\r\033[K${TAB}${RD}[ERROR]${CL} ${msg}"; exit 1; }

# Pre-flight Hypervisor Checks
check_root() {
  if [[ "$(id -u)" -ne 0 ]]; then
    msg_error "This script must be executed directly on the Proxmox VE host as root."
  fi
}

check_storage() {
  STORAGE="local-lvm"
  if ! pvesm status -storage "$STORAGE" &>/dev/null; then
    STORAGE="local"
  fi
}

configure_host_kernel() {
  msg_info "Loading required host kernel modules for traffic shaping"
  local modules=("sch_netem" "cls_u32" "ifb" "sch_tbf" "sch_prio")
  for mod in "${modules[@]}"; do
    modprobe "$mod" 2>/dev/null || true
    if ! grep -q "^${mod}$" /etc/modules-load.d/modules.conf 2>/dev/null; then
      echo "$mod" >> /etc/modules-load.d/modules.conf
    fi
  done
  msg_ok "Host kernel modules active and persisted"
}

configure_host_network() {
  msg_info "Checking isolated Linux bridge (vmbr2)"
  if ! grep -q "iface vmbr2" /etc/network/interfaces; then
    cat <<EOF >> /etc/network/interfaces

auto vmbr2
iface vmbr2 inet manual
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
    msg_ok "Isolated bridge vmbr2 provisioned"
  else
    msg_ok "Bridge vmbr2 is already configured"
  fi
}

fetch_template() {
  msg_info "Updating appliance index and downloading Debian 13 template"
  pveam update >/dev/null 2>&1
  
  # Search specifically for Debian 13 standard template
  TEMPLATE=$(pveam available -section system | awk '{print $2}' | grep "debian-13-standard" | head -n 1 || true)
  
  if [ -z "$TEMPLATE" ]; then
    # Fallback to current upstream standard if trixie package name varies in local mirror
    TEMPLATE=$(pveam available -section system | awk '{print $2}' | grep "debian" | sort -V | tail -n 1)
  fi

  if [ ! -f "/var/lib/vz/template/cache/${TEMPLATE}" ]; then
    pveam download local "$TEMPLATE" >/dev/null 2>&1
  fi
  msg_ok "Appliance template ready: ${TEMPLATE}"
}

create_container() {
  local ctid="$1"
  local name="$2"
  local internal_ip="$3"

  if pct status "$ctid" &>/dev/null; then
    msg_info "Container ${ctid} exists, destroying old instance"
    pct stop "$ctid" &>/dev/null || true
    pct destroy "$ctid" &>/dev/null
    msg_ok "Destroyed previous CT ${ctid}"
  fi

  msg_info "Creating CT ${ctid} (${name})"
  pct create "$ctid" "local:vztmpl/${TEMPLATE}" \
    --ostype debian \
    --hostname "$name" \
    --cores 2 \
    --memory 2048 \
    --swap 512 \
    --features nesting=1,keyctl=1 \
    --net0 name=eth0,bridge=vmbr0,ip=dhcp \
    --net1 name=eth1,bridge=vmbr2,ip="${internal_ip}/24" \
    --storage "$STORAGE" \
    --rootfs volume="${STORAGE}:8" \
    --unprivileged 0 >/dev/null 2>&1

  pct start "$ctid" >/dev/null 2>&1
  msg_ok "CT ${ctid} (${name}) created and running"
}

setup_container_base() {
  local ctid="$1"
  msg_info "Configuring base networking and toolchains inside CT ${ctid}"
  
  # Wait for systemd readiness and IP lease on eth0
  sleep 3
  
  pct exec "$ctid" -- bash -c "export DEBIAN_FRONTEND=noninteractive && \
    apt-get update -y >/dev/null && \
    apt-get install -y --no-install-recommends \
      iproute2 \
      iperf3 \
      iputils-ping \
      python3 \
      python3-pip \
      python3-venv \
      build-essential \
      git \
      curl \
      ca-certificates \
      gnupg >/dev/null 2>&1"
  msg_ok "Core utilities installed on CT ${ctid}"
}

setup_docker_engine() {
  local ctid="$1"
  msg_info "Deploying Docker CE runtime on CT ${ctid} (Controller/Historian)"
  
  pct exec "$ctid" -- bash -c "export DEBIAN_FRONTEND=noninteractive && \
    install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg --yes >/dev/null 2>&1 && \
    chmod a+r /etc/apt/keyrings/docker.gpg && \
    echo \"deb [arch=\$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian trixie stable\" > /etc/apt/sources.list.d/docker.list && \
    apt-get update -y >/dev/null && \
    apt-get install -y --no-install-recommends \
      docker-ce \
      docker-ce-cli \
      containerd.io \
      docker-buildx-plugin \
      docker-compose-plugin >/dev/null 2>&1 && \
    systemctl enable --now docker >/dev/null 2>&1"
    
  msg_ok "Docker daemon active inside CT ${ctid}"
}

# Main Execution Flow
header_info
check_root
check_storage
configure_host_kernel
configure_host_network
fetch_template

# Deploy Node 1 (Controller + Telemetry Host)
create_container 201 "ctrl-node-01" "10.10.10.1"
setup_container_base 201
setup_docker_engine 201

# Deploy Node 2 (Synthetic Plant)
create_container 202 "plant-node-01" "10.10.10.2"
setup_container_base 202

echo -e "\n${GN}${BOLD}Environment Setup Complete!${CL}"
echo -e "${TAB}${BOLD}Node 1 (Controller / Historian):${CL} CT 201 | eth1: 10.10.10.1/24 (Docker Ready)"
echo -e "${TAB}${BOLD}Node 2 (Plant / Actuator):      ${CL} CT 202 | eth1: 10.10.10.2/24"
echo -e "${TAB}${BL}Attach using:${CL} pct enter 201  ${BL}or${CL}  pct enter 202\n"