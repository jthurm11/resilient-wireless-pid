# Resilient Wireless Control: Hardening PID Loops against Network Jitter

A software-defined hardening and evaluation framework designed to quantify and mitigate the impact of non-deterministic wireless network dynamics (jitter, packet loss, and latency) on real-time Distributed Control Systems (DCS).

## Overview

Industrial IoT (IIoT) control loops operating over shared wireless channels (e.g., IEEE 802.11) face significant stability degradation from stochastic latency, packet loss, and channel contention. This framework enables:
- Programmatic injection of stochastic network impairments directly inside the Linux networking stack via kernel Traffic Control (`tc`) and Network Emulation (`netem`).
- Direct empirical benchmarking across Standard Discrete PID, Dead-Time Compensated Smith Predictor, and Predictive State-Estimating Resilient Controllers.
- Real-time sub-millisecond telemetry extraction into an InfluxDB v2/Grafana pipeline for stability boundary mapping and settling-time analysis.

## System Architecture

The testbed decouples real-time embedded control execution from human telemetry and supervisory orchestration across an isolated `10.10.10.0/24` subnet:

```mermaid
graph TD
    subgraph CtrlNode["Controller Node: dcs-ctrl-node (10.10.10.1)"]
        CLR["Control Loop Runtime<br/><i>(run-controller)</i>"]
        C2["C2 Orchestrator<br/><i>(Port :5050)</i>"]
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

* **Control Runtime Engine (`src/resilient_pid/controller/`)**: Discrete control laws supporting runtime switching between standard PID, Smith Predictor dead-time cancellation, and resilient observer estimation during dropouts.


* **Plant Abstraction Layer (`src/resilient_pid/plant/`)**: Polymorphic execution target supporting physical PWM/I2C peripheral drivers (`HardwarePlant`) or a 4th-Order Runge-Kutta continuous aerodynamic twin (`SimulatedPlant`).


* **Telemetry Pipeline (`src/resilient_pid/telemetry/`)**: Asynchronous, non-blocking ingestion client streaming process variables, setpoints, control efforts, and round-trip times to InfluxDB v2.


* **Command & Control Console (`src/resilient_pid/c2/`)**: REST API and operator web console for live setpoint adjustments, algorithm toggling, and trial coordination.



## Setup & Prerequisites

* **Target OS**: Debian 13 (Trixie) or Raspberry Pi OS (64-bit).


* **Kernel Modules**: `sch_netem`, `cls_u32` loaded into the active host/container kernel.


* **Runtimes**: Python 3.10+, Docker Engine 24.0+ (with Compose v2 plugin), and `iproute2`.


* **Privileges**: Elevated access (`sudo` or `CAP_NET_ADMIN`) for network queuing discipline manipulation.



> **Note on Virtual Testing:** To provision isolated Docker containers or Proxmox LXC testbeds with zero environment drift, refer to the [Mock Environment Guide](docs/development/mock_environment_guide.md).
> 

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

The time-series observability stack runs as containerized microservices managed via `scripts/infra/telemetry_stack.sh`:

```bash
# Provision InfluxDB v2 and Grafana
./scripts/infra/telemetry_stack.sh create

# Inspect container health and port bindings
./scripts/infra/telemetry_stack.sh status

# Validate telemetry pipeline with a synthetic smoke test
python3 scripts/test/telemetry_smoke_test.py
```

### 3. Verify Traffic Control Capabilities

Confirm that the kernel has loaded the network emulation scheduler and exposes netlink queueing discipline manipulation:

```bash
# Verify netem kernel module is loaded
sudo modprobe sch_netem

# Inspect active qdisc on inter-node DCS interface
tc qdisc show dev wlan0
```


## Quickstart Trial

Follow this procedure to run an initial closed-loop test across the testbed:

### 1. Plant Node (`dcs-plant-node` @ 10.10.10.2)

Start the plant runtime daemon listening on UDP port `5005`:

```bash
run-plant --mode simulate --host 0.0.0.0 --port 5005
```

### 2. Controller Node (`dcs-ctrl-node` @ 10.10.10.1)

Identify your active egress interface pointing to the plant node (e.g., `wlan0`):

```bash
IFACE="wlan0"
```

Launch the real-time controller runtime to establish the nominal baseline:

```bash
run-controller --mode baseline --setpoint 50.0 --enable-ui
```

* **Grafana Dashboard:** Navigate to `http://127.0.0.1:3000` (`admin`/`admin`) to inspect real-time tracking error, control effort, and baseline round-trip times.


* **C2 Operator UI:** Navigate to `http://127.0.0.1:5050` to adjust setpoints or switch control algorithms dynamically.


* **Headless C2 Execution:** Update parameters programmatically via REST:


```bash
curl -s -X POST [http://127.0.0.1:5050/api/control](http://127.0.0.1:5050/api/control) \
  -H "Content-Type: application/json" \
  -d '{"algorithm": "smith", "setpoint": 65.0}'
```

### 3. Inject Network Degradation

With the loop stabilized, apply stochastic network degradation to the egress interface to observe destabilization in Grafana:

```bash
# Inject 40ms baseline delay, ±10ms Gaussian jitter, and 2% packet loss
sudo tc qdisc add dev $IFACE root netem delay 40ms 10ms distribution normal loss 2%

# Verify active queue rules and transit latency
tc -s qdisc show dev $IFACE
ping -c 5 10.10.10.2
```

### 4. Clear Network Emulation

Restore the interface to line-rate execution:

```bash
sudo tc qdisc del dev $IFACE root
```

---

## Attribution & Prior Art

This project is an advanced research continuation developed for the Master of Engineering Capstone at the University of Connecticut.

* **Preceding Implementation**: System concepts, architectural foundations, and hardware-in-the-loop insights originated from [`jthurm11/iot-real-time-scheduler-evaluation`](https://github.com/jthurm11/iot-real-time-scheduler-evaluation).


* **Physical Testbed Design**: Baseline physical plant and ball-floating topology adapted from the PingPongPID research testbed by [`Salzmann (2025)`](https://doi.org/10.1021/acs.jchemed.5c00528).


## License

This project is licensed under the terms of the [MIT License](LICENSE).
