# Distributed Control Systems (DCS) Testbed Operations & Replication Guide

This guide details the standard procedure for executing dynamic baseline evaluations, automated network impairment sweeps, emergency fail-safe shutdowns, and data post-processing. 

The primary replication target is the **Local Virtual Twin (Docker)**, which uses a continuous 4th-order Runge-Kutta aerodynamic software simulation and requires no specialized embedded hardware. Secondary deployment environments (Proxmox LXC and Bare-Metal Raspberry Pi) use identical socket interfaces, CLI semantics, and directory layouts.

For environment provisioning, container topology details, and lifecycle commands, see the [Virtual Environment Guide](virtual_environment_guide.md).

---

## 1. System Invariants & Environment Mapping

The framework enforces identical node naming, interface semantics, and network addressing across all deployment modes:
* **Control Node (`dcs-ctrl-node`):** `10.10.10.1/24` on `wlan0`. Hosts the real-time controller runtime, C2 supervisory engine (port `5050`), Linux Traffic Control (`tc`/`netem`), and automated test runners.
* **Plant Node (`dcs-plant-node`):** `10.10.10.2/24` on `wlan0`. Hosts the plant dynamic listener (UDP port `5005`).

| Parameter | Primary: Local Docker Twin | Secondary: Proxmox LXC (CT 201/202) | Secondary: Bare-Metal Raspberry Pi |
| :--- | :--- | :--- | :--- |
| **Execution Context** | `docker exec -it dcs-ctrl-node` | `pct exec 201` or SSH | SSH (`admin@dcs-ctrl-node`) |
| **Plant Backend** | Continuous Aerodynamic Twin | Continuous Aerodynamic Twin | Physical Column + EMC2101 + HC-SR04 |
| **Process Isolation** | Detached POSIX Subprocess (`docker exec -d`) | Transient Systemd Unit (`systemd-run`) | Transient Systemd Unit (`systemd-run`) |
| **Log Ingestion** | File Stream (`data_exports/test_execution.log`) | Systemd Journal (`journalctl -u`) | Systemd Journal (`journalctl -u`) |
| **Data Synchronization** | Automatic (Local Host Bind-Mount) | Network Sync (`fetch_datasets.sh`) | Network Sync (`fetch_datasets.sh`) |

---

## 2. Pre-Flight Verification

Ensure the testbed services are operational prior to executing trials:

```bash
# Query C2 state engine (run from host workspace or control node shell):
# Optionally query remote C2 target: ./scripts/c2_ctl.sh -t <NODE_IP> status
./scripts/c2_ctl.sh status

# For physical hardware deployments only: execute actuator/sensor sanity check:
# ssh -t admin@dcs-plant-node "sudo python3 /opt/resilient-wireless-pid/src/resilient_pid/plant/plant_interface.py --op-test --ignore-tach --service-restart"

```

---

## 3. Empirical Test Execution

All dynamic evaluations are executed from `dcs-ctrl-node` using calibrated defaults within the linear aerodynamic flight corridor ($SP \in [48\%, 70\%]$).

To ensure unattended test runs persist across SSH network disconnections and terminal drops without process multiplexer fragility, executions use **isolated systemd transient units** (Proxmox/Bare-Metal) or **detached background processes** (Docker).

> [!NOTE]
> **CLI Options:** Script defaults are pre-configured for standardized runs. Pass `-h` to view optional parameters (e.g., custom durations, algorithms, or setpoint profiles):
> ```bash
> python3 scripts/test/run_baselines.py -h
> python3 scripts/test/run_sweeps.py -h
> 
> ```
> 
> 

---

### 3.1 Dynamic Multi-Step Baseline (`run_baselines.py`)

Evaluates line-rate tracking performance across Discrete PID, Smith Predictor, and Resilient PID over a standardized step sequence ($SP: 60\% \to 65\% \to 70\% \to 48\% \to 65\% \to 55\%$).

#### Primary Target: Local Docker Twin

Launch detached from the host terminal; logs stream to disk via bind mount:

```bash
# 1. Launch detached runner process
docker exec -d \
  -w /opt/resilient-wireless-pid \
  -e PYTHONUNBUFFERED=1 \
  dcs-ctrl-node \
  bash -c "python3 scripts/test/run_baselines.py > data_exports/test_execution.log 2>&1"

# 2. Monitor real-time output (from host repository root or inside container)
tail -f ./data_exports/test_execution.log

```

#### Secondary Target: Proxmox VE (LXC CT 201)

Launch via transient unit inside CT 201 (from hypervisor host or container shell):

```bash
# 1. Launch transient systemd unit
pct exec 201 -- systemd-run \
  --unit=dcs-baseline-runner \
  --description="DCS Baseline Step Response Suite" \
  --working-directory=/opt/resilient-wireless-pid \
  --setenv=PYTHONUNBUFFERED=1 \
  python3 scripts/test/run_baselines.py

# 2. Stream execution log in real time
pct exec 201 -- journalctl -u dcs-baseline-runner -f -o cat

```

#### Secondary Target: Bare-Metal Raspberry Pi

Launch via transient systemd unit on the physical control node over SSH:

```bash
# 1. Launch transient systemd unit
ssh admin@dcs-ctrl-node \
  "sudo systemd-run \
    --unit=dcs-baseline-runner \
    --description='DCS Baseline Step Response Suite' \
    --working-directory=/opt/resilient-wireless-pid \
    --setenv=PYTHONUNBUFFERED=1 \
    /opt/resilient-wireless-pid/.venv/bin/python3 scripts/test/run_baselines.py"

# 2. Stream execution log in real time (safe to detach/reconnect at any time)
ssh admin@dcs-ctrl-node "journalctl -u dcs-baseline-runner -f -o cat"

```

---

### 3.2 Network Impairment Parameter Sweeps (`run_sweeps.py`)

Executes the empirical network perturbation matrix across three stress vectors:

1. **Vector 1 (Transport Latency):** $10\text{ ms} \le \text{RTT} \le 100\text{ ms}$ (Discrete PID vs. Smith Predictor).
2. **Vector 2 (Stochastic Jitter):** Normal and Pareto distributions at $40\text{ ms} \pm 15\text{ ms}$ (Discrete vs. Smith vs. Resilient).
3. **Vector 3 (Burst Drops):** Gilbert-Elliott loss profiles $2\% \le \text{Loss} \le 20\%$ (Discrete vs. Resilient).

#### Primary Target: Local Docker Twin

```bash
# 1. Launch detached runner process
docker exec -d \
  -w /opt/resilient-wireless-pid \
  -e PYTHONUNBUFFERED=1 \
  dcs-ctrl-node \
  bash -c "python3 scripts/test/run_sweeps.py > data_exports/test_execution.log 2>&1"

# 2. Monitor real-time output
tail -f ./data_exports/test_execution.log

```

#### Secondary Target: Proxmox VE (LXC CT 201)

```bash
# 1. Launch transient systemd unit
pct exec 201 -- systemd-run \
  --unit=dcs-sweep-runner \
  --description="DCS Network Impairment Parameter Sweeps" \
  --working-directory=/opt/resilient-wireless-pid \
  --setenv=PYTHONUNBUFFERED=1 \
  python3 scripts/test/run_sweeps.py

# 2. Stream execution log in real time
pct exec 201 -- journalctl -u dcs-sweep-runner -f -o cat

```

#### Secondary Target: Bare-Metal Raspberry Pi

```bash
# 1. Launch transient systemd unit
ssh admin@dcs-ctrl-node \
  "sudo systemd-run \
    --unit=dcs-sweep-runner \
    --description='DCS Network Impairment Parameter Sweeps' \
    --working-directory=/opt/resilient-wireless-pid \
    --setenv=PYTHONUNBUFFERED=1 \
    /opt/resilient-wireless-pid/.venv/bin/python3 scripts/test/run_sweeps.py"

# 2. Stream execution log in real time
ssh admin@dcs-ctrl-node "journalctl -u dcs-sweep-runner -f -o cat"

```

---

## 4. Fail-Safe Shutdown & Emergency Recovery

When an evaluation diverges or must be halted prematurely, two actions must occur: zeroing actuator effort ($u = 0.0\%$) and flushing kernel `tc` queuing disciplines on `wlan0`.

### 4.1 In-Band Graceful Interrupt

If attached directly to the log streamer or foreground shell, issue **`Ctrl + C`** (`SIGINT`). The test harness traps this signal, stops C2 actuation, and executes kernel `tc` cleanup automatically.

### 4.2 Out-of-Band Emergency Takedown

If the test runner hangs, diverges, or must be aborted while running detached:

#### Local Docker Twin:

```bash
# 1. Terminate running Python test harness
docker exec dcs-ctrl-node pkill -f run_sweeps.py || true
docker exec dcs-ctrl-node pkill -f run_baselines.py || true

# 2. Command zero-actuation failsafe via C2
./scripts/c2_ctl.sh stop

# 3. Flush orphaned kernel traffic control rules
docker exec dcs-ctrl-node tc qdisc del dev wlan0 root 2>/dev/null || true

```

#### Proxmox VE (LXC CT 201):

```bash
# 1. Stop the transient systemd units
pct exec 201 -- systemctl stop dcs-sweep-runner 2>/dev/null || true
pct exec 201 -- systemctl stop dcs-baseline-runner 2>/dev/null || true

# 2. Command zero-actuation failsafe via C2
./scripts/c2_ctl.sh -t <CT201_IP> stop

# 3. Flush orphaned kernel traffic control rules
pct exec 201 -- tc qdisc del dev wlan0 root 2>/dev/null || true

```

#### Bare-Metal Raspberry Pi:

```bash
# 1. Stop the transient systemd units
ssh admin@dcs-ctrl-node "sudo systemctl stop dcs-sweep-runner 2>/dev/null || true; sudo systemctl stop dcs-baseline-runner 2>/dev/null || true"

# 2. Command zero-actuation failsafe via C2
./scripts/c2_ctl.sh -t <RPI_CTRL_IP> stop

# 3. Flush orphaned kernel traffic control rules
ssh admin@dcs-ctrl-node "sudo tc qdisc del dev wlan0 root 2>/dev/null || true"

# 4. Hardware Fail-Safe: Direct Actuator Cutoff (if controller daemon is hung)
# ssh admin@dcs-plant-node "sudo python3 -c 'import smbus2; b=smbus2.SMBus(1); b.write_byte_data(0x4C, 0x4C, 0x00); b.close()'"

```

---

## 5. Post-Processing & Data Extraction

The analytical pipeline processes raw InfluxDB trial CSV snapshots, applies standardized physical geometry schemas, computes control performance indicators (ITAE, RMSE, Settling Time, Maximum Overshoot, Loss Rate), and formats thesis-ready tables.

### 5.1 Execute Post-Processing Pipeline

Run the aggregation script to compile raw trial snapshots into partitioned vector datasets:

```bash
# Primary Target (Docker - executed natively from host workstation due to bind-mount):
python3 scripts/analysis/aggregate_and_postprocess.py \
  --raw-dir ./data_exports \
  --output-dir ./data_exports/processed \
  --environment docker

# Secondary Targets (Executed directly on the remote control node):
# python3 scripts/analysis/aggregate_and_postprocess.py

```

### 5.2 Synchronize Datasets (Remote Targets Only)

Under the primary Docker environment, data written to `/opt/resilient-wireless-pid/data_exports` is linked via bind-mount directly to `./data_exports` on the host, making network transfers unnecessary.

For remote targets, pull the processed datasets to the local host workspace:

```bash
# Validate Docker local bind-mount state (zero-copy check):
./scripts/analysis/fetch_datasets.sh docker

# Synchronize from remote Proxmox hypervisor:
./scripts/analysis/fetch_datasets.sh proxmox <PROXMOX_HOST_IP>

# Synchronize from physical Raspberry Pi:
./scripts/analysis/fetch_datasets.sh rpi <NODE_IP>

```

### 5.3 Output Artifacts

Post-processing yields standardized artifacts in `./data_exports/processed/`:

* `vector0_dynamic_baselines_<tube>_<env>_<timestamp>.csv`: Baseline step-response trajectories.
* `vector1_deadtime_sweeps_<tube>_<env>_<timestamp>.csv`: Delay-induced stability boundaries.
* `vector2_stochastic_jitter_<tube>_<env>_<timestamp>.csv`: Latency distribution evaluations.
* `vector3_burst_loss_<tube>_<env>_<timestamp>.csv`: Packet drop resilience trajectories.
* `thesis_control_performance_kpis.csv`: Comprehensive KPI summary table across all evaluated permutations.

