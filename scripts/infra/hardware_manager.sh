#!/usr/bin/env bash
# scripts/infra/hardware_manager.sh
# Provisions, inspects, and decommissions physical Raspberry Pi nodes for the resilient DCS testbed.
# Configures secondary network aliases, systemd daemons, and GPIO status monitors.

set -Eeuo pipefail

# ANSI color formatting
YW="\033[33m"
BL="\033[36m"
RD="\033[01;31m"
GN="\033[1;92m"
CL="\033[m"
BOLD="\033[1m"
TAB="  "

# Network and storage paths
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." 2>/dev/null && pwd || echo "")"
TARGET_DIR="/opt/resilient-wireless-pid"
STATE_FILE="/etc/dcs_node_role"
FALLBACK_HOSTNAME="raspberrypi"
DCS_SUBNET_MASK="24"
CTRL_IP="10.10.10.1"
PLANT_IP="10.10.10.2"

# BCM pin assignments
GPIO_GREEN=17  # Pin 11
GPIO_RED=27    # Pin 13

SHOW_HEADER=true
TARGET_ROLE=""
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
  echo -e "${BL}${BOLD}Distributed Control System - Bare-Metal Node Setup${CL}\n"
}

print_usage() {
  # Renders CLI usage and option descriptions to stdout.
  cat <<EOF
Usage: $0 [ctrl|plant] {create|destroy|status} [options]

Commands:
  create          Configure host networking, dependencies, and systemd units
  destroy         Purge services, revert hostname, and remove network aliases
  status          Interrogate node interfaces, peripheral buses, and services

Positional Arguments:
  role            Node operational target: 'ctrl' (10.10.10.1) or 'plant' (10.10.10.2)
                  Required for 'create' unless state file exists. Optional for 'status'/'destroy'.

Options:
  -h, --help      Display this help message and exit
  -q, --no-header Suppress ASCII header display

Examples:
  sudo $0 ctrl create
  sudo $0 plant create
  sudo $0 status
  sudo $0 destroy
EOF
}

msg_info()  { echo -ne "${TAB}${YW}[INFO]${CL} $1..."; }
msg_ok()    { echo -e "\r\033[K${TAB}${GN}[OK]${CL} $1"; }
msg_warn()  { echo -e "\r\033[K${TAB}${YW}[WARN]${CL} $1"; }
msg_error() { echo -e "\r\033[K${TAB}${RD}[ERROR]${CL} $1"; exit 1; }

parse_cli_arguments() {
  # Parses CLI positional arguments, flags, and infers active node role.
  # Args: $@ (raw command line parameters)
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
      ctrl|plant)
        TARGET_ROLE="$arg"
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

  # Auto-resolve target role from persistent state file or active unit
  if [ -z "$TARGET_ROLE" ]; then
    if [ -f "$STATE_FILE" ]; then
      TARGET_ROLE="$(cat "$STATE_FILE")"
    elif systemctl is-active --quiet dcs-controller.service || [ -f "/etc/systemd/system/dcs-controller.service" ]; then
      TARGET_ROLE="ctrl"
    elif systemctl is-active --quiet dcs-plant.service || [ -f "/etc/systemd/system/dcs-plant.service" ]; then
      TARGET_ROLE="plant"
    elif [ "$ACTION" == "create" ]; then
      msg_error "Node role [ctrl|plant] required for initial creation. Run '$0 --help'."
    fi
  fi
}

resolve_repository_workspace() {
  # Ensures repository assets and manifests are available on disk.
  if [ ! -f "${REPO_ROOT}/pyproject.toml" ]; then
    msg_info "Manifests missing locally. Bootstrapping repository into ${TARGET_DIR}"
    apt-get update -y >/dev/null 2>&1
    apt-get install -y --no-install-recommends git >/dev/null 2>&1
    if [ ! -d "$TARGET_DIR" ]; then
      git clone https://github.com/jthurm11/resilient-wireless-pid.git "$TARGET_DIR" >/dev/null 2>&1
    else
      git -C "$TARGET_DIR" pull >/dev/null 2>&1 || true
    fi
    REPO_ROOT="$TARGET_DIR"
    msg_ok "Repository synchronized at ${REPO_ROOT}"
  fi
}

check_execution_environment() {
  # Verifies superuser execution rights and processor architecture.
  if [ "$(id -u)" -ne 0 ]; then
    msg_error "Requires root privileges. Re-run using 'sudo $0'."
  fi

  local arch
  arch="$(uname -m)"
  if [[ "$arch" != "armv7l" && "$arch" != "aarch64" ]]; then
    msg_warn "Non-ARM architecture ($arch) detected. Physical buses may be absent."
  fi
}

find_wifi_interface() {
  # Identifies primary operational wireless network interface name.
  # Returns: interface string
  local iface
  iface="$(ip -o link show | awk -F': ' '{print $2}' | grep -E '^wlan|^wl' | head -n 1 || true)"
  echo "${iface:-wlan0}"
}

configure_secondary_network() {
  # Applies secondary static IPv4 address alias to wireless interface.
  # Args: $1 (role: 'ctrl' or 'plant')
  local role="$1"
  local iface
  iface="$(find_wifi_interface)"
  local target_ip=""

  [ "$role" == "ctrl" ] && target_ip="$CTRL_IP" || target_ip="$PLANT_IP"

  msg_info "Configuring secondary alias ${target_ip}/${DCS_SUBNET_MASK} on ${iface}"
  
  ip addr del "${target_ip}/${DCS_SUBNET_MASK}" dev "$iface" 2>/dev/null || true
  ip addr add "${target_ip}/${DCS_SUBNET_MASK}" dev "$iface" label "${iface}:dcs"

  if command -v nmcli >/dev/null 2>&1 && systemctl is-active --quiet NetworkManager; then
    local conn_name
    conn_name="$(nmcli -t -f NAME,DEVICE connection show --active | grep ":${iface}$" | cut -d: -f1 | head -n 1 || true)"
    if [ -n "$conn_name" ]; then
      nmcli connection modify "$conn_name" +ipv4.addresses "${target_ip}/${DCS_SUBNET_MASK}" 2>/dev/null || true
    fi
  elif [ -d "/etc/network" ]; then
    mkdir -p /etc/network/interfaces.d
    cat <<EOF > /etc/network/interfaces.d/dcs-alias
auto ${iface}:dcs
iface ${iface}:dcs inet static
    address ${target_ip}
    netmask 255.255.255.0
EOF
  fi

  msg_ok "DCS control alias bound to ${iface}:dcs (${target_ip})"
}

remove_secondary_network() {
  # Removes persistent interface configurations and flushes aliases.
  local iface
  iface="$(find_wifi_interface)"
  
  msg_info "Flushing DCS subnet aliases from ${iface}"
  ip addr del "${CTRL_IP}/${DCS_SUBNET_MASK}" dev "$iface" 2>/dev/null || true
  ip addr del "${PLANT_IP}/${DCS_SUBNET_MASK}" dev "$iface" 2>/dev/null || true

  if command -v nmcli >/dev/null 2>&1 && systemctl is-active --quiet NetworkManager; then
    local conn_name
    conn_name="$(nmcli -t -f NAME,DEVICE connection show --active | grep ":${iface}$" | cut -d: -f1 | head -n 1 || true)"
    if [ -n "$conn_name" ]; then
      nmcli connection modify "$conn_name" -ipv4.addresses "${CTRL_IP}/${DCS_SUBNET_MASK}" 2>/dev/null || true
      nmcli connection modify "$conn_name" -ipv4.addresses "${PLANT_IP}/${DCS_SUBNET_MASK}" 2>/dev/null || true
    fi
  fi

  rm -f /etc/network/interfaces.d/dcs-alias
  
  msg_ok "Subnet aliases removed"
}

configure_hostname() {
  # Updates kernel hostname and aligns local loopback host mappings.
  # Args: $1 (target hostname)
  local target_hostname="$1"
  msg_info "Setting system hostname to '${target_hostname}'"
  hostnamectl set-hostname "$target_hostname"
  
  sed -i "s/127\.0\.1\.1.*/127.0.1.1\t${target_hostname}/g" /etc/hosts
  if ! grep -q "127.0.1.1" /etc/hosts; then
    echo -e "127.0.1.1\t${target_hostname}" >> /etc/hosts
  fi
  msg_ok "Hostname and /etc/hosts synchronized"
}

restore_hostname() {
  msg_info "Resetting hostname to '${FALLBACK_HOSTNAME}'"
  hostnamectl set-hostname "$FALLBACK_HOSTNAME"
  sed -i "s/127\.0\.1\.1.*/127.0.1.1\t${FALLBACK_HOSTNAME}/g" /etc/hosts
  msg_ok "Default hostname restored"
}

enable_hardware_buses() {
  msg_info "Enabling kernel peripheral bus overlays"
  local config_txt="/boot/firmware/config.txt"
  [ ! -f "$config_txt" ] && config_txt="/boot/config.txt"

  if [ -f "$config_txt" ]; then
    if ! grep -q "^dtparam=i2c_arm=on" "$config_txt"; then
      echo "dtparam=i2c_arm=on" >> "$config_txt"
    fi
  fi
  
  if command -v dtparam >/dev/null 2>&1; then
    dtparam i2c_arm=on 2>/dev/null || true
  elif command -v dtoverlay >/dev/null 2>&1; then
    dtoverlay i2c-arm 2>/dev/null || true
  fi

  modprobe i2c-dev 2>/dev/null || true
  modprobe sch_netem 2>/dev/null || true

  if [ ! -e "/dev/i2c-1" ]; then
    msg_warn "/dev/i2c-1 not yet active. A reboot may be required if runtime dtparam failed."
  else
    msg_ok "I2C (/dev/i2c-1) and netem modules enabled"
  fi
}

deploy_led_monitor() {
  # Deploys gpiozero-backed diagnostic state daemon as a systemd service.
  # Args: $1 (role: 'ctrl' or 'plant')
  local role="$1"
  local peer_ip=""
  [ "$role" == "ctrl" ] && peer_ip="$PLANT_IP" || peer_ip="$CTRL_IP"

  msg_info "Deploying hardware diagnostic LED monitor daemon"
  
  cat <<EOF > /usr/local/bin/dcs-led-monitor.py
#!/usr/bin/env python3
"""
dcs-led-monitor.py
Supervises runtime daemon health and peer network reachability via status LEDs.
"""
import subprocess
import time
from gpiozero import LED

PIN_GREEN = ${GPIO_GREEN}
PIN_RED = ${GPIO_RED}
PEER_IP = "${peer_ip}"
ROLE = "${role}"

led_green = LED(PIN_GREEN)
led_red = LED(PIN_RED)

def check_link() -> bool:
    """Checks bidirectional peer reachability over the DCS network."""
    cmd = ["ping", "-c", "1", "-W", "1", PEER_IP]
    return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0

def check_daemon() -> bool:
    """Queries systemd unit status for the local DCS runtime service."""
    unit = "dcs-controller.service" if ROLE == "ctrl" else "dcs-plant.service"
    return subprocess.run(["systemctl", "is-active", "--quiet", unit]).returncode == 0

try:
    while True:
        daemon_ok = check_daemon()
        link_ok = check_link()

        if daemon_ok and link_ok:
            led_green.on()
            led_red.off()
            time.sleep(1.0)
        elif daemon_ok and not link_ok:
            led_green.off()
            led_red.on()
            time.sleep(0.5)
        else:
            led_green.off()
            led_red.toggle()
            time.sleep(0.2)
except KeyboardInterrupt:
    pass
finally:
    led_green.off()
    led_red.off()
    led_green.close()
    led_red.close()
EOF
  chmod +x /usr/local/bin/dcs-led-monitor.py

  cat <<EOF > /etc/systemd/system/dcs-led-monitor.service
[Unit]
Description=DCS Hardware Diagnostic LED Monitor
After=network.target

[Service]
Type=simple
User=root
ExecStart=/usr/bin/python3 /usr/local/bin/dcs-led-monitor.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable --now dcs-led-monitor.service >/dev/null 2>&1
  msg_ok "Diagnostic LED supervisor online"
}

provision_workspace() {
  # Installs system packages, prepares venv, and compiles repo in editable mode.
  msg_info "Configuring repository virtual environment and dependencies"
  apt-get update -y >/dev/null 2>&1
  apt-get install -y --no-install-recommends \
    python3-pip python3-venv python3-gpiozero python3-libgpiod i2c-tools iproute2 >/dev/null 2>&1

  cd "$REPO_ROOT"
  [ ! -d ".venv" ] && python3 -m venv .venv
  source .venv/bin/activate
  pip install --upgrade pip >/dev/null 2>&1
  pip install -r requirements.txt >/dev/null 2>&1
  pip install -e . >/dev/null 2>&1
  msg_ok "Dependencies installed and package registered"
}

deploy_node_service() {
  # Deploys and initializes real-time systemd service units for node role.
  # Args: $1 (role: 'ctrl' or 'plant')
  local role="$1"
  local unit_name=""
  
  if [ "$role" == "ctrl" ]; then
    unit_name="dcs-controller.service"
    msg_info "Deploying controller runtime and telemetry stack"
    bash "${REPO_ROOT}/scripts/infra/telemetry_stack.sh" create --no-header >/dev/null 2>&1 || true

    cat <<EOF > /etc/systemd/system/${unit_name}
[Unit]
Description=DCS Real-Time Controller and Orchestrator
After=network-online.target dcs-led-monitor.service
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=${REPO_ROOT}
ExecStart=${REPO_ROOT}/.venv/bin/python -m resilient_pid.main --enable-ui
Restart=on-failure
RestartSec=5
CPUSchedulingPolicy=rr
CPUSchedulingPriority=85

[Install]
WantedBy=multi-user.target
EOF
  else
    unit_name="dcs-plant.service"
    msg_info "Deploying plant physical runtime daemon"
    cat <<EOF > /etc/systemd/system/${unit_name}
[Unit]
Description=DCS Plant Physical Runtime Daemon
After=network-online.target dcs-led-monitor.service
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=${REPO_ROOT}
ExecStart=${REPO_ROOT}/.venv/bin/python -m resilient_pid.plant.plant_interface --mode hardware --host 0.0.0.0 --port 5005
Restart=always
RestartSec=3
CPUSchedulingPolicy=rr
CPUSchedulingPriority=80

[Install]
WantedBy=multi-user.target
EOF
  fi

  systemctl daemon-reload
  systemctl enable --now "$unit_name" >/dev/null 2>&1
  msg_ok "Real-time service activated (${unit_name})"
}

action_create() {
  # Executes end-to-end bare-metal provisioning workflow.
  local role="$TARGET_ROLE"
  local target_hostname="dcs-${role}-node"

  echo -e "${BOLD}Provisioning Raspberry Pi Bare-Metal Node [Role: ${role^^}]...${CL}"
  check_execution_environment
  enable_hardware_buses
  configure_hostname "$target_hostname"
  configure_secondary_network "$role"
  resolve_repository_workspace
  provision_workspace
  deploy_led_monitor "$role"
  deploy_node_service "$role"

  echo "$role" > "$STATE_FILE"

  echo -e "\n${GN}${BOLD}Node Setup Complete.${CL}"
  echo -e "${TAB}${BOLD}Hostname:${CL}  $(hostname)"
  echo -e "${TAB}${BOLD}Interface:${CL} $(find_wifi_interface):dcs"
  echo -e "${TAB}${BOLD}DCS IPv4:${CL}  $([ "$role" == "ctrl" ] && echo "$CTRL_IP" || echo "$PLANT_IP")"
  echo -e "${TAB}${BOLD}Service:${CL}   $([ "$role" == "ctrl" ] && echo "dcs-controller.service" || echo "dcs-plant.service")\n"
}

action_destroy() {
  # Decommissions DCS services, restores default hostname, and purges aliases.
  echo -e "${BOLD}Decommissioning Node and Purging DCS Configurations...${CL}"
  
  for unit in dcs-controller.service dcs-plant.service dcs-led-monitor.service; do
    if systemctl is-active --quiet "$unit" || [ -f "/etc/systemd/system/${unit}" ]; then
      msg_info "Disabling ${unit}"
      systemctl stop "$unit" 2>/dev/null || true
      systemctl disable "$unit" 2>/dev/null || true
      rm -f "/etc/systemd/system/${unit}"
      msg_ok "Removed ${unit}"
    fi
  done
  systemctl daemon-reload

  # De-energize indicators
  python3 -c "
try:
    from gpiozero import LED
    LED(${GPIO_GREEN}).close()
    LED(${GPIO_RED}).close()
except Exception:
    pass
" 2>/dev/null || true

  # Tear down telemetry containers if on controller node
  if [ -f "${REPO_ROOT}/scripts/infra/telemetry_stack.sh" ]; then
    bash "${REPO_ROOT}/scripts/infra/telemetry_stack.sh" destroy --no-header >/dev/null 2>&1 || true
  fi

  remove_secondary_network
  restore_hostname
  rm -f "$STATE_FILE"

  echo -e "\n${GN}${BOLD}Node Teardown Complete. Factory parameters restored.${CL}\n"
}

action_status() {
  # Interrogates and displays node runtime parameters and peripheral health.
  local role="${TARGET_ROLE:-unknown}"
  local iface
  iface="$(find_wifi_interface)"
  local unit=""
  [ "$role" == "ctrl" ] && unit="dcs-controller.service"
  [ "$role" == "plant" ] && unit="dcs-plant.service"

  echo -e "${BOLD}Bare-Metal DCS Node Diagnostic Report:${CL}\n"
  echo -e "${TAB}${BOLD}Active Node Role:${CL}     ${role^^}"
  echo -e "${TAB}${BOLD}Host System:${CL}          $(hostname) ($(uname -m))"
  
  # Network alias query
  local ip_configured
  ip_configured="$(ip -4 addr show dev "$iface" | grep -oE "10\.10\.10\.[12]" || echo "NONE")"
  echo -e "${TAB}${BOLD}DCS Secondary IP:${CL}     ${ip_configured} on ${iface}"

  # Runtime unit status
  if [ -n "$unit" ]; then
    local status_line
    status_line="$(systemctl is-active "$unit" 2>/dev/null || echo "inactive")"
    echo -e "${TAB}${BOLD}Runtime Daemon:${CL}       ${unit} (${status_line})"
  fi

  local led_status
  led_status="$(systemctl is-active dcs-led-monitor.service 2>/dev/null || echo "inactive")"
  echo -e "${TAB}${BOLD}Diagnostic Supervisor:${CL} dcs-led-monitor.service (${led_status})"

  # Peripheral I2C bus probe
  if command -v i2cdetect >/dev/null 2>&1; then
    local i2c_check
    i2c_check="$(i2cdetect -y 1 2>/dev/null | grep -E "4c|29" || true)"
    echo -e "${TAB}${BOLD}Detected I2C Devices:${CL} ${i2c_check:-No peripherals detected}"
  fi

  # Telemetry stack status on controller node
  if [ "$role" == "ctrl" ] && [ -f "${REPO_ROOT}/scripts/infra/telemetry_stack.sh" ]; then
    echo -e "\n${BOLD}Telemetry Microservice Stack:${CL}"
    bash "${REPO_ROOT}/scripts/infra/telemetry_stack.sh" status --no-header
  fi
  echo ""
}

header_info
parse_cli_arguments "$@"

case "$ACTION" in
  create)  action_create ;;
  destroy) action_destroy ;;
  status)  action_status ;;
esac