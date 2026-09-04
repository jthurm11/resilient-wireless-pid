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

### Option 1: One-Step Provision

> [!CAUTION]  
> The following command prevents you from reading the code before executing it on your system. Please review the code before installation.  

```bash
bash -c "$(wget -qLO - https://raw.githubusercontent.com/jthurm11/resilient-wireless-pid/main/scripts/infra/proxmox_mock_provisioner.sh)"
```

### Option 2: Manually download the provisioner and run

```bash
wget -q https://raw.githubusercontent.com/jthurm11/resilient-wireless-pid/main/scripts/infra/proxmox_mock_provisioner.sh 
bash proxmox_mock_provisioner.sh
```

### What the Provisioner Configures Automatically

1. **Kernel Queuing Modules**: Checks, dynamically attaches, and persists `sch_netem`, `sch_tbf`, `sch_prio`, `cls_u32`, and `ifb` via `/etc/modules-load.d/modules.conf`.
2. **Hypervisor Datapath**: Creates and reloads `vmbr2` across the PVE host network stack.
3. **LXC Configuration**: Deploys **Priveleged** CT 201 and CT 202 based on Debian 13 standard rootfs templates with `nesting=1` and `keyctl=1` flags.
4. **Base System Packages**: Installs `iproute2`, `iperf3`, `python3-venv`, and essential toolchains across both nodes.
5. **Node 201 Container Runtime**: Installs and enables the official Docker CE daemon inside CT 201.

---

## 2. Container Lifecycle & Workspace Initialization

Once the provisioner completes, refer to the project root documentation (`README.md`) to initialize the Python workspace, install runtime requirements, and provision telemetry services via Docker.

### 2.1 Accessing the Target Containers

Attach to each node via the PVE console or host terminal:

```bash
# Attach to Node 1 (Controller / Historian)
pct enter 201

# Attach to Node 2 (Plant)
pct enter 202
```

### 2.2 Validating Traffic Control & Network Emulation

The kernel queuing modules are active and managed via netlink. Run the following checks on CT 201 (`ctrl-node-01`) to verify traffic manipulation on the isolated `eth1` interface without impacting `eth0` management: 

```bash
# Apply synthetic network impairment: 40ms baseline delay, ±10ms jitter, and 2% packet loss
tc qdisc add dev eth1 root netem delay 40ms 10ms distribution normal loss 2%

# Verify active queuing disciplines and validate transit degradation across the DCS link
tc -s qdisc show dev eth1
ping -c 20 10.10.10.2

# Teardown active netem rules and restore default interface discipline
tc qdisc del dev eth1 root
```

Expected round-trip times will fluctuate within the interval $[30\text{ ms}, 50\text{ ms}]$, with dropped packet metrics registering in the ping transmission report.

### 2.3 Teardown and Resource Cleanup

To decommission the mock testbed environment, stop container runtimes and purge both container definitions from host storage directly from the PVE host shell:

```bash
# Terminate and purge container instances
pct stop 201 && pct destroy 201
pct stop 202 && pct destroy 202
```
