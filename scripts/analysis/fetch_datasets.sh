#!/usr/bin/env bash
# scripts/analysis/fetch_datasets.sh
# Synchronizes raw or post-processed CSV datasets from remote DCS environments
# (Proxmox LXC / Raspberry Pi) or validates local Docker bind-mounts.

set -Eeuo pipefail

BL="\033[36m"
GN="\033[1;92m"
YW="\033[33m"
RD="\033[01;31m"
BOLD="\033[1m"
CL="\033[m"
TAB="  "

msg_info()    { printf "${TAB}${GN}[INFO]${CL} %s\n" "$1"; }
msg_warn()    { printf "${TAB}${YW}[WARN]${CL} %s\n" "$1"; }
msg_error()   { printf "${TAB}${RD}[ERROR]${CL} %s\n" "$1" >&2; exit 1; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." 2>/dev/null && pwd)"
LOCAL_DIR="${REPO_ROOT}/data_exports"
REMOTE_PATH="/opt/resilient-wireless-pid/data_exports"

print_usage() {
    printf "${BL}${BOLD}DCS Dataset Synchronization Utility${CL}\n\n"
    printf "Usage: %s [docker | proxmox <pve_ip> | rpi [node_ip]] [--processed]\n\n" "$0"
    printf "Environments:\n"
    printf "  docker                Validates host bind-mount (zero network transfer required)\n"
    printf "  proxmox <pve_ip>      Synchronizes from CT 201 via hypervisor SSH using rsync\n"
    printf "  rpi [node_ip]         Synchronizes from bare-metal Pi via SSH (default: 10.10.10.1)\n\n"
    printf "Options:\n"
    printf "  --processed           Sync only processed datasets (default: syncs entire export root)\n"
    exit 1
}

TARGET_ENV="${1:-}"
[ -z "$TARGET_ENV" ] && print_usage

SUBDIR=""
if [[ "${*:-}" =~ "--processed" ]]; then
    SUBDIR="/processed"
fi

mkdir -p "${LOCAL_DIR}${SUBDIR}"

case "$TARGET_ENV" in
  docker)
    msg_info "Checking Docker container 'dcs-ctrl-node'..."
    if ! docker ps --format '{{.Names}}' | grep -q "^dcs-ctrl-node$"; then
      msg_error "Container 'dcs-ctrl-node' is not running."
    fi

    IS_MOUNTED=$(docker inspect dcs-ctrl-node --format '{{range .Mounts}}{{if eq .Destination "/opt/resilient-wireless-pid/data_exports"}}{{.Type}}{{end}}{{end}}')

    if [[ "$IS_MOUNTED" == "bind" ]]; then
      COUNT=$(find "${LOCAL_DIR}${SUBDIR}" -maxdepth 1 -name "*.csv" | wc -l | tr -d ' ')
      msg_info "Local bind-mount verified: ${LOCAL_DIR} <-> ${REMOTE_PATH}"
      msg_info "Zero-copy active. ${COUNT} CSV artifact(s) available directly on host at: ${LOCAL_DIR}${SUBDIR}"
    else
      msg_warn "Bind mount not detected. Extracting via docker cp..."
      docker cp "dcs-ctrl-node:${REMOTE_PATH}${SUBDIR}/." "${LOCAL_DIR}${SUBDIR}/"
      msg_info "Artifacts copied successfully."
    fi
    ;;

  proxmox)
    PVE_HOST="${2:-}"
    [ -z "$PVE_HOST" ] && msg_error "Proxmox mode requires hypervisor IP/host. Example: $0 proxmox 192.168.1.50"
    msg_info "Synchronizing datasets from Proxmox CT 201 (${PVE_HOST})..."
    rsync -avzP \
      -e "ssh -o StrictHostKeyChecking=accept-new" \
      --include="*/" --include="*.csv" --exclude="*" \
      "root@${PVE_HOST}:/var/lib/lxc/201/rootfs${REMOTE_PATH}${SUBDIR}/" \
      "${LOCAL_DIR}${SUBDIR}/"
    msg_info "Proxmox dataset synchronization complete."
    ;;

  rpi)
    RPI_HOST="${2:-10.10.10.1}"
    msg_info "Synchronizing datasets from Raspberry Pi (${RPI_HOST})..."
    rsync -avzP \
      -e "ssh -o StrictHostKeyChecking=accept-new" \
      --include="*/" --include="*.csv" --exclude="*" \
      "admin@${RPI_HOST}:${REMOTE_PATH}${SUBDIR}/" \
      "${LOCAL_DIR}${SUBDIR}/"
    msg_info "Raspberry Pi dataset synchronization complete."
    ;;

  *)
    print_usage
    ;;
esac