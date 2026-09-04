# Proxmox VE Isolated Mock Environment Setup

This guide details the deployment of a dual-node Linux Container (LXC) testbed on Proxmox Virtual Environment (PVE). This environment enables pre-hardware simulation and validation of telemetry pipelines prior to flashing physical edge nodes.

---

## Architecture Overview

The mock testbed models an edge Distributed Control System (DCS) by isolating deterministic control datagrams from hypervisor management traffic. Two lightweight LXC nodes are provisioned on an isolated Linux bridge (`vmbr2`). Management and SSH access remain segmented on `vmbr0` (PVE default bridge interface).

```

+-------------------------------------------------------------------------------+
|                                Proxmox VE Host                                |
|                                                                               |
|   +---------------------------------+      +------------------------------+   |
|   | CT 201: ctrl-node-01            |      | CT 202: plant-node-01        |   |
|   | (PID Runtime + Telemetry Stack) |      | (Synthetic Plant / Actuator) |   |
|   |                                 |      |                              |   |
|   | eth0: vmbr0 (Management)        |      | eth0: vmbr0 (Management)     |   |
|   | eth1: 10.10.10.1/24             |      | eth1: 10.10.10.2/24          |   |
|   +-----------------+---------------+      +---------------+--------------+   |
|                     |                                      |                  |
|                     +------ Isolated Bridge (vmbr2) -------+                  |
+-------------------------------------------------------------------------------+

```

---

## 1. Automated Provisioning

An automated deployment script handles kernel module persistence, `vmbr2` creation, template acquisition, LXC instantiation, and container-level system dependency installation.

From the **Proxmox VE Host root shell**, run:

```bash
bash -c "$(wget -qLO - https://raw.githubusercontent.com/jthurm11/resilient-wireless-pid/main/scripts/infra/proxmox_provision_mock.sh)"

```

### 1.1 Persist Required Kernel Modules
Linux Traffic Control (`tc`) and Network Emulation (`netem`) require specific queuing modules loaded into the host kernel:

```bash
# Load modules immediately
modprobe sch_netem
modprobe sch_tbf
modprobe sch_prio
modprobe cls_u32
modprobe ifb

# Persist modules across reboots
cat <<EOF>> /etc/modules-load.d/modules.conf
sch_netem
sch_tbf
sch_prio
cls_u32
ifb
EOF

```

### 1.2 Provision the Isolated Linux Bridge (`vmbr2`)

Append the internal test network definition to `/etc/network/interfaces`:

```bash
cat <<EOF>> /etc/network/interfaces

auto vmbr2
iface vmbr2 inet manual
        bridge-ports none
        bridge-stp off
        bridge-fd 0
# Isolated test network for distributed control traffic
EOF

# Apply network configuration without rebooting
ifreload -a || systemctl restart networking

```

---

## 2. Container Provisioning

We utilize `debian-12-standard` to mirror the systemd, glibc, and package topology of Raspberry Pi OS (Bookworm). Privileged mode (`--unprivileged 0`) is employed to ensure uninterrupted access to the netlink interface and queuing disciplines.

### 2.1 Download Template and Deploy Containers

```bash
# Update template cache and download Debian 12
pveam update
pveam download local debian-12-standard_12.7-1_amd64.tar.zst

# Node 1: Controller Runtime Node (CT 201)
pct create 201 local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst \
  --ostype debian \
  --hostname ctrl-node-01 \
  --cores 2 \
  --memory 2048 \
  --swap 512 \
  --features nesting=1 \
  --net0 name=eth0,bridge=vmbr0,ip=dhcp \
  --net1 name=eth1,bridge=vmbr2,ip=10.10.10.1/24 \
  --storage local-lvm \
  --rootfs volume=local-lvm:8 \
  --unprivileged 0

# Node 2: Synthetic Plant/Actuator Node (CT 202)
pct create 202 local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst \
  --ostype debian \
  --hostname plant-node-01 \
  --cores 2 \
  --memory 2048 \
  --swap 512 \
  --features nesting=1 \
  --net0 name=eth0,bridge=vmbr0,ip=dhcp \
  --net1 name=eth1,bridge=vmbr2,ip=10.10.10.2/24 \
  --storage local-lvm \
  --rootfs volume=local-lvm:8 \
  --unprivileged 0

# Start both instances
pct start 201
pct start 202

```

---

## 3. Node Environment Setup

Execute these steps inside both containers (`pct enter 201` and `pct enter 202`).

### 3.1 Install Toolchains and Dependencies

```bash
apt update && apt install -y \
  iproute2 \
  iperf3 \
  python3 \
  python3-pip \
  python3-venv \
  git \
  curl \
  build-essential

# Verify inter-node private connectivity
# Run from CT 201:
ping -c 3 10.10.10.2

# Run from CT 202:
ping -c 3 10.10.10.1

```

### 3.2 Workspace Initialization

```bash
# Clone the repository
git clone https://github.com/jthurm11/resilient-wireless-pid.git /opt/resilient-wireless-pid
cd /opt/resilient-wireless-pid

# Setup isolated virtual environment
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

```

---

## 4. Verification of Traffic Control & Network Emulation

To verify that the kernel queuing disciplines operate correctly inside the container namespace, execute the following validation tests on `ctrl-node-01` (`CT 201`).

### 4.1 Apply Jitter and Latency Injection

Inject a baseline dead-time delay of $40\text{ ms} \pm 10\text{ ms}$ (normal distribution) with a $2\%$ loss rate strictly on the isolated control interface (`eth1`):

```bash
tc qdisc add dev eth1 root netem delay 40ms 10ms distribution normal loss 2%

```

### 4.2 Validate Egress Shaping Metrics

```bash
# Verify active qdisc parameters
tc -s qdisc show dev eth1

# Transmit ICMP probes to plant node (10.10.10.2)
ping -c 20 10.10.10.2

```

Observe the round-trip times to confirm that delays fall within the $[30\text{ ms}, 50\text{ ms}]$ boundary and that dropped packets occur.

### 4.3 Teardown Emulation Rules

Reset the interface back to default `pfifo_fast` or `fq_codel`:

```bash
tc qdisc del dev eth1 root

```

---

## 5. Teardown and Rebuild Automation

To completely tear down the virtual environment:

```bash
# Execute from Proxmox VE host shell
pct stop 201 && pct destroy 201
pct stop 202 && pct destroy 202

```
