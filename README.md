# Resilient Wireless Control: Hardening PID Loops against Network Jitter

A software-defined hardening and evaluation framework designed to quantify and mitigate the impact of non-deterministic wireless network dynamics (jitter, packet loss, and latency) on real-time Distributed Control Systems (DCS).

---

## Overview

Industrial IoT (IIoT) control loops operating over shared wireless channels (e.g., IEEE 802.11) face significant stability degradation from stochastic latency, packet loss, and channel contention. This framework enables:
- Programmatic injection of stochastic network impairments directly inside the Linux networking stack via kernel Traffic Control (`tc`) and Network Emulation (`netem`).
- Direct empirical benchmarking across Standard Discrete PID, Dead-Time Compensated Smith Predictor, and Event-Triggered/State-Estimating Resilient Controllers.
- Real-time sub-millisecond telemetry extraction into an InfluxDB/Grafana pipeline for stability boundary mapping and settling-time analysis.

---

## System Architecture

The testbed decouples real-time embedded control execution from human telemetry and data aggregation across an isolated `10.10.10.0/24` subnet:


```

+---------------------------------------------------------------------------------+
| Controller Node: dcs-ctrl-node (10.10.10.1)                                     |
|                                                                                 |
|   +----------------------------+           +--------------------------------+   |
|   | Control Runtime (main.py)  | <===> | C2 Orchestrator (c2_server.py) |   |
|   | (PID / Smith / Resilient)  |           | (REST API / Web Console :5000) |   |
|   +-------------+--------------+           +--------------------------------+   |
|                 | (via 127.0.0.1:8086)                                          |
|                 v                                                               |
|   +----------------------------+           +--------------------------------+   |
|   | InfluxDB v2 Engine (:8086) | <===> | Grafana Visualizer (:3000)     |   |
|   +----------------------------+           +--------------------------------+   |
|                 |                                                               |
|                 +--- [ Linux Kernel tc/netem qdisc (eth0 / eth1) ]              |
+---------------------------------------+-----------------------------------------+
|
Isolated DCS Subnet (10.10.10.0/24 UDP)
|
+---------------------------------------+-----------------------------------------+
| Plant Node: dcs-plant-node (10.10.10.2)                                         |
|                                                                                 |
|   +-------------------------------------------------------------------------+   |
|   | Plant Runtime (plant_interface.py :5005 UDP)                            |   |
|   | - Simulated Mode: Continuous Aerodynamic ODE Twin (RK4 Integration)     |   |
|   | - Hardware Mode:  Physical GPIO/PWM Fan & I2C Distance Sensor Bus       |   |
|   +-------------------------------------------------------------------------+   |
+---------------------------------------------------------------------------------+

```

```mermaid
graph TD
    subgraph CtrlNode["Controller Node: dcs-ctrl-node (10.10.10.1)"]
        CLR["Control Loop Runtime<br/><i>(run-controller)</i>"]
        C2["C2 Orchestrator<br/><i>(Port :5000)</i>"]
        INF["InfluxDB v2 Engine<br/><i>(Port :8086)</i>"]
        GRA["Grafana Dashboards<br/><i>(Port :3000)</i>"]
        TC["Kernel tc/netem qdisc<br/><i>(Egress Shaping)</i>"]

        CLR <-->|Internal Sockets / IPC| C2
        CLR -->|Line Protocol| INF
        INF --> GRA
        CLR --> TC
    end

    subgraph PlantNode["Plant Node: dcs-plant-node (10.10.10.2)"]
        PLANT["Plant Runtime<br/><i>(run-plant :5005 UDP)</i>"]
    end

    TC <==>|Stochastic Wireless Link| PLANT

    classDef default fill:#f9f9f9,stroke:#333,stroke-width:1px;
    classDef nodeBox fill:#eef3f8,stroke:#1f497d,stroke-width:2px;
    class CtrlNode,PlantNode nodeBox;

```

### Core Components 

1. **Control Runtime Engine (`src/resilient_pid/controller/`)**: Discrete control laws supporting runtime switching between standard PID, Smith Predictor dead-time cancellation, and resilient observer estimation during dropouts. 
2. **Plant Hardware/Simulation Abstraction (`src/resilient_pid/plant/`)**: Polymorphic execution target supporting physical PWM/I2C peripheral drivers or a continuous aerodynamic software twin. 
3. **Telemetry Pipeline (`src/resilient_pid/telemetry/`)**: Asynchronous non-blocking client streaming process variables ($PV$), setpoints ($SP$), control efforts ($u(t)$), and round-trip times ($RTT$) to InfluxDB v2. 
4. **Command & Control Console (`src/resilient_pid/c2/`)**: REST API and operator web console for live setpoint adjustments, algorithm toggling, and trial coordination. 

---

## Setup & Prerequisites

### Requirements 

- **Target OS**: Debian 13 (Trixie) or Raspberry Pi OS (64-bit). 
- **Kernel Modules**: `sch_netem`, `cls_u32` loaded into the active kernel. 
- **Runtimes**: Python 3.10+, Docker Engine (for telemetry services), and `iproute2`. 
- **Privileges**: Elevated access (`sudo` or `CAP_NET_ADMIN`) for network queuing manipulations. 

> **Note on Virtual Testing:** To validate control routines, testbed topologies, and Linux `tc/netem` rules without physical hardware, refer to the [Mock Environment Guide](docs/development/mock_environment_guide.md). 


### 1. Workspace Provisioning 

```bash
git clone https://github.com/jthurm11/resilient-wireless-pid.git
cd resilient-wireless-pid

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install -e .
```

### 2. Launch Telemetry Infrastructure

The time-series observability stack (InfluxDB v2 and Grafana) runs as containerized microservices managed via `scripts/infra/telemetry_stack.sh`: 

```bash
# Provision InfluxDB v2 and Grafana
bash ./scripts/infra/telemetry_stack.sh create

# Inspect container health and port bindings
bash ./scripts/infra/telemetry_stack.sh status

# Stop and purge telemetry containers
bash ./scripts/infra/telemetry_stack.sh destroy
```

### 3. Verify Traffic Control Capabilities

Confirm that the active Linux kernel has loaded the network emulation scheduler and exposes netlink queueing discipline manipulation:

```bash
# Verify netem kernel module is active
sudo modprobe sch_netem

# Inspect active qdisc on inter-node DCS interface
tc qdisc show dev eth0
```

---

## Quickstart Trial

Follow this procedure to run an initial closed-loop test across the testbed:

### 1. Plant Node (`dcs-plant-node` @ 10.10.10.2)

Start the plant runtime daemon listening on UDP port `5005` (use `--mode simulate` when testing without physical hardware):

```bash
run-plant --mode simulate --host 0.0.0.0 --port 5005

```

### 2. Controller Node (`dcs-ctrl-node` @ 10.10.10.1)

Apply stochastic network degradation to the outbound DCS link interface:

```bash
# Inject 40ms baseline delay, ±10ms Gaussian jitter, and 2% packet loss
sudo tc qdisc add dev eth0 root netem delay 40ms 10ms distribution normal loss 2%

# Verify transit delay to dcs-plant-node
ping -c 5 10.10.10.2

```

Launch the real-time controller runtime (spawning background C2 orchestration with the UI enabled):

```bash
run-controller --mode baseline --setpoint 50.0 --enable-ui

```

* **C2 Operator UI:** Navigate to `http://localhost:5000` (or `http://10.10.10.1:5000`) to adjust setpoints or switch control algorithms on the fly.


* **Grafana Dashboard:** Navigate to `http://localhost:3000` (or `http://10.10.10.1:3000`) (`admin`/`admin`) to inspect tracking error and network transit metrics.



### 3. Clear Network Emulation

Restore the interface to default line-rate transmission:

```bash
sudo tc qdisc del dev eth0 root

```

---

## Attribution & Prior Art

This project is an advanced research continuation developed for the Master of Engineering Capstone at the University of Connecticut. 


* **Preceding Implementation**: System concepts, architectural foundations, and hardware-in-the-loop insights originated from [`jthurm11/iot-real-time-scheduler-evaluation`](https://github.com/jthurm11/iot-real-time-scheduler-evaluation). 
* **Physical Testbed Design**: Baseline physical plant and ball-floating topology adapted from the PingPongPID research testbed by [`Salzmann (2025)`](https://doi.org/10.1021/acs.jchemed.5c00528). 


## License

This project is licensed under the terms of the [MIT License](LICENSE). 
