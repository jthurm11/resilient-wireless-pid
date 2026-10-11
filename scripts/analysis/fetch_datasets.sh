#!/usr/bin/env bash
# scripts/analysis/fetch_datasets.sh
# Synchronizes post-processed experimental CSV datasets from remote execution environments.

set -Eeuo pipefail

# ANSI Color Codes
BL="\033[36m"     # Cyan (Debug)
GN="\033[1;92m"   # Green (Info)
YW="\033[33m"     # Yellow (Warning)
RD="\033[01;31m"  # Red (Error)
MG="\033[1;35m"   # Magenta (Critical)
BOLD="\033[1m"    # Bold (Structural Headers)
CL="\033[m"       # Clear
TAB="  "

# Standardized Logging Functions
msg_debug()    { printf "${TAB}${BL}[DEBUG]${CL} %s\n" "$1"; }
msg_info()     { printf "${TAB}${GN}[INFO]${CL} %s\n" "$1"; }
msg_warning()  { printf "${TAB}${YW}[WARNING]${CL} %s\n" "$1"; }
msg_error()    { printf "\r\033[K${TAB}${RD}[ERROR]${CL} %s\n" "$1" >&2; }
msg_critical() { printf "\r\033[K${TAB}${MG}[CRITICAL]${CL} %s\n" "$1" >&2; exit 1; }

# Default paths
REMOTE_DIR="/opt/resilient-wireless-pid/data_exports/processed"
LOCAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." 2>/dev/null && pwd)/data_exports/processed"

print_usage() {
    printf "${BL}${BOLD}Distributed Control System - Dataset Synchronization${CL}\n\n"
    printf "Usage: %s [user@target_ip]...\n" "$0"
    printf "Synchronizes processed dataset exports from remote DCS nodes to the local workspace.\n\n"
    printf "${BOLD}Example:${CL}\n"
    printf "  %s pi@10.10.10.1 root@10.10.10.2\n" "$0"
    exit 1
}

# Enforce input arguments; print usage and exit if none are provided
if [ "$#" -eq 0 ]; then
    print_usage
fi

printf "${BL}${BOLD}Distributed Control System - Dataset Synchronization${CL}\n\n"

# Ensure local destination directory exists
mkdir -p "$LOCAL_DIR"

# Arrays to track the state of batch execution
successful_targets=()
failed_targets=()

for target in "$@"; do
    msg_info "Initiating secure transfer from ${target}:${REMOTE_DIR}..."
    
    if rsync -avzP \
        -e "ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=5" \
        --include="*.csv" \
        --exclude="*" \
        "${target}:${REMOTE_DIR}/" \
        "${LOCAL_DIR}/"; then
        
        msg_info "Successfully synchronized datasets from ${target}"
        successful_targets+=("$target")
    else
        msg_error "Synchronization failed for ${target}. Verify network and credentials."
        failed_targets+=("$target")
    fi
    printf "\n"
done

#  Execution Summary
printf "${BOLD}--- Batch Synchronization Summary ---${CL}\n"

if [ "${#failed_targets[@]}" -eq 0 ]; then
    msg_info "All ${#successful_targets[@]} targets synchronized successfully."
elif [ "${#successful_targets[@]}" -eq 0 ]; then
    # Utilizing msg_critical halts the script with exit code 1
    msg_critical "All ${#failed_targets[@]} targets failed to synchronize."
else
    msg_warning "Partial success. Synchronized: ${#successful_targets[@]} | Failed: ${#failed_targets[@]}"
    msg_error "Failed nodes: ${failed_targets[*]}"
    # Exit with a non-zero status code to indicate the batch did not completely succeed
    exit 1
fi

if [ "${#successful_targets[@]}" -gt 0 ]; then
    msg_debug "Datasets are available at: $LOCAL_DIR"
fi