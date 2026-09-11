# Mock Environment Setup Guide

This procedure configures an isolated dual-node Distributed Control System (DCS) testbed for pre-hardware simulation and validation. It ensures complete runtime parity with the bare-metal Raspberry Pi environment: deterministic socket addressing, isolated subnets (`10.10.10.0/24`), Linux kernel queue disciplines (`tc/netem`), and time-series telemetry logging.

The only functional deviation from bare metal is the plant runtime: virtual testbeds execute a continuous software physics twin (`run-plant --mode simulate`) instead of interacting with physical sensors and PWM fan drivers (`run-plant --mode hardware`).

Both Docker and Proxmox LXC tracks establish identical socket endpoints and network boundaries:
* **`dcs-ctrl-node`:** `10.10.10.1` (`127.0.0.1` for intra-node IPC)
  * `10.10.10.1:5000` (TCP/HTTP) $\to$ Command & Control (C2) Orchestrator API / Web Console
  * `10.10.10.1:8086` (TCP/HTTP) $\to$ InfluxDB v2 Line Protocol Engine
  * `10.10.10.1:3000` (TCP/HTTP) $\to$ Grafana Dashboards
* **`dcs-plant-node`:** `10.10.10.2`
  * `10.10.10.2:5005` (UDP) $\to$ Dynamic state-space listener (`step(u) -> pv`)


## Unified Testbed Management (`mock_environment.sh`)

All provisioning, health verification, and teardown workflows are managed through the centralized orchestrator `scripts/infra/mock_environment.sh`.  

* **Automatic Backend Detection:** The script inspects the host runtime. When executed on a development workstation with an active Docker daemon, it defaults to the **Docker** backend. When run on a Proxmox VE hypervisor host shell (detecting `pct` and `pvesm`), it defaults to the **LXC** backend.  
* **Explicit Targeting:** You can override auto-detection by passing `docker` or `lxc` explicitly as the first parameter.

```text
Usage:
  ./scripts/infra/mock_environment.sh [docker|lxc] {create|destroy|status} [--no-header]
```


## Deployment Architectures

Two isolated execution targets are supported:

1. **Docker (Local Workstation):** Runs the dual-node DCS environment locally via Docker Compose.
    * Control plane services (`influxdb`, `grafana`, `controller`) share the network namespace of `dcs-ctrl-node` (`10.10.10.1`), while `dcs-plant-node` executes in a dedicated container (`10.10.10.2`). 
    * The orchestrator automatically creates the `10.10.10.0/24` network bridge, initializes InfluxDB v2, passes container healthchecks, mounts Grafana dashboards, and starts the real-time controller and plant runtimes automatically as container entrypoints. 
    * *Prerequisites:* Docker Engine 24.0+ and Docker Compose v2 plugin (`docker compose`). 


2. **Proxmox VE (LXC Testbed):** Deploys two Debian 13 containers on a Proxmox VE host connected via an isolated Linux software bridge (`vmbr1`). 
    * The orchestrator persists hypervisor kernel modules (`sch_netem`, `ifb`, `cls_u32`), provisions `vmbr1`, creates CT 201 (`dcs-ctrl-node`) and CT 202 (`dcs-plant-node`), configures virtualenvs, and starts the telemetry stack inside CT 201 via `telemetry_stack.sh`. 
    * Control loops are invoked interactively inside each container session. 


### Step 0: Prepare the Host

Clone the source repository onto your preferred target host and enter the repository root:

```bash
git clone https://github.com/jthurm11/resilient-wireless-pid.git
cd resilient-wireless-pid
```

> [!NOTE]  
> All orchestration scripts must be executed from the **repository root directory** (`resilient-wireless-pid/`).


### Step 1: Provision the Testbed

Deploy the environment:

```bash
./scripts/infra/mock_environment.sh create
# Explicit overrides:
#   ./scripts/infra/mock_environment.sh docker create
#   ./scripts/infra/mock_environment.sh lxc create
```

Verify service health and container status:

```bash
./scripts/infra/mock_environment.sh status
```


### Step 2: Access Nodes & Execute Control Loops

#### Option A: Docker Deployment

In Docker, the controller and simulated plant loops start automatically on boot.

1. **Inspect Active Control Logs:**
```bash
# Follow real-time controller execution
docker compose -f deploy/docker/docker-compose.yml logs -f controller

# Follow simulated plant physics
docker compose -f deploy/docker/docker-compose.yml logs -f dcs-plant-node
```


2. **Interactive Node Shell Access (Optional):**
```bash
# Attach to Control Node Pod (10.10.10.1)
docker exec -it dcs-ctrl-node bash

# Attach to Plant Node Container (10.10.10.2)
docker exec -it dcs-plant-node bash
```



#### Option B: Proxmox LXC Deployment

In Proxmox, open two separate terminal sessions on the PVE host shell to start the control loop:

**Terminal 1 (`dcs-plant-node`):**

```bash
pct enter 202
cd /opt/resilient-wireless-pid && source .venv/bin/activate
run-plant --mode simulate
```

**Terminal 2 (`dcs-ctrl-node`):**

```bash
pct enter 201
cd /opt/resilient-wireless-pid && source .venv/bin/activate
run-controller --enable-ui
```


### Step 3: Inject Kernel Network Emulation (`tc/netem`)

Open a terminal session inside **`dcs-ctrl-node`** to apply network degradation to the outbound DCS interface:

```bash
# Set interface target
IFACE="wlan0"

# Inject 40ms baseline delay, ±10ms jitter, and 2% packet loss
tc qdisc add dev $IFACE root netem delay 40ms 10ms distribution normal loss 2%

# Inspect active queue rules and test transit delay to dcs-plant-node
tc -s qdisc show dev $IFACE
ping -c 10 10.10.10.2

# Restore default line-rate transmission
tc qdisc del dev $IFACE root
```


### Step 4: Decommissioning

To stop containers, release network namespaces, and purge storage volumes:

```bash
./scripts/infra/mock_environment.sh destroy
# Explicit overrides:
#   ./scripts/infra/mock_environment.sh docker destroy
#   ./scripts/infra/mock_environment.sh lxc destroy
```
