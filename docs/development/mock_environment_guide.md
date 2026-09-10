# Mock Environment Setup Procedure (Proxmox LXC & Docker)

This procedure configures an isolated dual-node Distributed Control System (DCS) testbed for pre-hardware simulation and validation. It ensures complete runtime parity with the bare-metal Raspberry Pi environment: deterministic socket addressing, isolated subnets (`10.10.10.0/24`), Linux kernel queue disciplines (`tc/netem`), and time-series telemetry logging.

The only functional deviation from bare metal is the plant runtime: virtual testbeds execute a continuous software physics twin (`run-plant --mode simulate`) instead of interacting with physical sensors and PWM fan drivers (`run-plant --mode hardware`).

Both Docker and Proxmox LXC tracks establish identical socket endpoints and network boundaries: 
* **`dcs-ctrl-node`:** `10.10.10.1` (`127.0.0.1` for intra-node IPC) 
  * `10.10.10.1:5000` (TCP/HTTP) $\to$ Command & Control (C2) Orchestrator API / Web Console 
  * `10.10.10.1:8086` (TCP/HTTP) $\to$ InfluxDB v2 Line Protocol Engine 
  * `10.10.10.1:3000` (TCP/HTTP) $\to$ Grafana Dashboards 
* **`dcs-plant-node`:** `10.10.10.2` 
  * `10.10.10.2:5005` (UDP) $\to$ Dynamic state-space listener (`step(u) -> pv`) 

---

## Unified Testbed Management (`mock_environment.sh`)

All provisioning, health verification, and teardown workflows are managed through the centralized script `scripts/infra/mock_environment.sh`.  

* **Automatic Backend Detection:** The script automatically inspects the execution environment. When run directly on a Proxmox VE hypervisor host shell (detecting `pct` and `pvesm`), it targets the **LXC** backend. When run on a development workstation with an active Docker daemon, it targets the **Docker** backend. 
* **Explicit Targeting:** You can override auto-detection by passing `docker` or `lxc` explicitly as the first argument.

```text
Usage:
  ./scripts/infra/mock_environment.sh [docker|lxc] {create|destroy|status} [--no-header]

```

---

## Track A: Local Docker Deployment

Run the dual-node DCS environment locally using Docker Compose. Control plane services share the network namespace of `dcs-ctrl-node` (`10.10.10.1`), while `dcs-plant-node` executes in an independent container (`10.10.10.2`).

### Prerequisites

* Docker Engine 24.0+ and Docker Compose v2 plugin (`docker compose`) installed and running.
* Repository cloned locally:
```bash
git clone [https://github.com/jthurm11/resilient-wireless-pid.git](https://github.com/jthurm11/resilient-wireless-pid.git)
cd resilient-wireless-pid

```



> **Working Directory Note**: All script commands must be executed from the **repository root directory** (`resilient-wireless-pid/`).

### Step 1: Provision the Testbed

Deploy the stack:

```bash
./scripts/infra/mock_environment.sh create
# Or explicitly: ./scripts/infra/mock_environment.sh docker create

```

This automatically configures the `10.10.10.0/24` network bridge, initializes InfluxDB v2, passes container healthchecks, mounts auto-provisioned Grafana dashboards, and starts the real-time controller and plant runtimes.

Check running container status:

```bash
./scripts/infra/mock_environment.sh status

```

### Step 2: Access Container Terminals

Open two separate terminal windows from the repository root:

**Terminal 1 (`dcs-plant-node`):**

```bash
docker exec -it dcs-plant-node bash

```

**Terminal 2 (`dcs-ctrl-node`):**

```bash
docker exec -it dcs-ctrl-node bash

```

### Step 3: Inject Kernel Network Emulation (`tc/netem`)

Inside **Terminal 2 (`dcs-ctrl-node`)**, apply network degradation directly to the egress interface (`eth0`):

```bash
# Inject 40ms baseline delay, ±10ms jitter, and 2% packet loss
tc qdisc add dev eth0 root netem delay 40ms 10ms distribution normal loss 2%

# Inspect active qdisc rules and test transit delay to dcs-plant-node
tc -s qdisc show dev eth0
ping -c 10 10.10.10.2

# Restore default line-rate transmission
tc qdisc del dev eth0 root

```

### Step 4: Decommissioning

To stop containers, remove the network bridge, and prune anonymous volumes:

```bash
./scripts/infra/mock_environment.sh destroy

```

---

## Track B: Proxmox VE (LXC Testbed)

Deploy two Debian 13 containers on a Proxmox VE host connected via an isolated Linux software bridge (`vmbr1`).

### Step 1: Provision the Testbed

Log in to the **Proxmox VE Host root shell**, clone the repository, and run the manager:

```bash
git clone [https://github.com/jthurm11/resilient-wireless-pid.git](https://github.com/jthurm11/resilient-wireless-pid.git) /root/resilient-wireless-pid
cd /root/resilient-wireless-pid
./scripts/infra/mock_environment.sh create
# Or explicitly: ./scripts/infra/mock_environment.sh lxc create

```

The script persists host kernel modules (`sch_netem`, `ifb`, `cls_u32`), provisions the `vmbr1` internal bridge, deploys CT 201 (`dcs-ctrl-node`) and CT 202 (`dcs-plant-node`), sets up Python virtual environments, and initializes the telemetry stack via `telemetry_stack.sh` on CT 201.

### Step 2: Access Container Consoles

Open two terminal sessions on your Proxmox VE host:

**Terminal 1 (`dcs-plant-node`):**

```bash
pct enter 202

```

**Terminal 2 (`dcs-ctrl-node`):**

```bash
pct enter 201

```

### Step 3: Execute Closed-Loop Control

Inside **Terminal 1 (`dcs-plant-node` / CT 202)**:

```bash
cd /opt/resilient-wireless-pid && source .venv/bin/activate
run-plant --mode simulate

```

Inside **Terminal 2 (`dcs-ctrl-node` / CT 201)**:

```bash
cd /opt/resilient-wireless-pid && source .venv/bin/activate
run-controller --with-c2 --enable-ui

```

### Step 4: Inject Kernel Network Emulation (`tc/netem`)

Inside **Terminal 2 (`dcs-ctrl-node` / CT 201)**, apply traffic shaping to the isolated bridge interface (`eth1`):

```bash
# Inject 40ms baseline delay, ±10ms jitter, and 2% packet loss
tc qdisc add dev eth1 root netem delay 40ms 10ms distribution normal loss 2%

# Verify queue parameters and ping dcs-plant-node
tc -s qdisc show dev eth1
ping -c 10 10.10.10.2

# Clear emulation rules
tc qdisc del dev eth1 root

```

### Step 5: Decommissioning

From the **Proxmox VE Host root shell**, destroy both test containers:

```bash
cd /root/resilient-wireless-pid
./scripts/infra/mock_environment.sh destroy

```
