# Testbed Operations & Trial Execution Procedure

This document defines repeatable test execution steps for running the **84 standardized trial scenarios** across the three core control architectures:
- **`discrete`**: Standard Discrete PID (`DiscretePID`) with derivative filtering ($\tau_f = 0.35\text{ s}$) and anti-windup clamping.
- **`smith`**: Smith Predictor dead-time compensator (`SmithPredictor`) with 2nd-order discrete ARX plant model and circular delay buffer.
- **`resilient`**: Resilient Predictive Observer PID (`ResilientPID`) with 1-step recursive ARX prediction and integral freezing ($\Delta I = 0$) during packet dropouts (`is_loss = True`).

## Execution Matrix

All experimental stages are executed sequentially from **Stage 0** through **Stage 4**.

| Trial ID | Impairment Scenario | Test Script | Variable Parameters | Success Criteria |
| :--- | :--- | :--- | :--- | :--- |
| **Stage 0** | Quiet Baseline | `run_baselines.py` | Unshaped link ($RTT \approx 0.5\text{--}1.2\text{ ms}$, loss = $0\%$) | 9 unperturbed baseline trials logged ($N=3$ per algorithm). |
| **Stage 1** | Pure Dead-Time ($\theta$) | `run_sweeps.py` | Delay $\theta \in [10, 20, \dots, 100\text{ ms}]$ via `tc/netem` | 30 delay sweeps logged across all control laws ($N=10$ per algorithm). |
| **Stage 2** | Stochastic Jitter ($\sigma$) | `run_sweeps.py` | Fixed $\bar{\theta} = 40\text{ ms}$; Jitter $\sigma \in [5, 10, 20\text{ ms}]$ (Normal & Pareto) | 18 jitter/distribution sweeps logged across all control laws ($N=6$ per algorithm). |
| **Stage 3** | Burst Packet Loss ($p_{\text{loss}}$) | `run_sweeps.py` | Fixed $\bar{\theta} = 40\text{ ms}, \sigma = 10\text{ ms}$; Loss $p_{\text{loss}} \in [2\%, 5\%, 10\%, 15\%, 20\%]$ | 15 packet loss sweeps logged across all control laws ($N=5$ per algorithm). |
| **Stage 4** | Adversary Cross-Traffic | `iperf3` + `run_baselines.py` | Unshaped link + UDP cross-traffic ($2, 5, 10\text{ Mbps}$, Saturation) | 12 cross-traffic saturation trials logged ($N=4$ per algorithm). | 

**Overall Success Criterion:** All 84 trials completed and ingested into InfluxDB `wireless_pid_metrics`. 

---

## 1. Pre-Conditions

Deploy and verify the targeted runtime environment per its setup guide **in its entirety** before starting execution.

> [!CAUTION]  
> Do NOT execute any teardown, cleanup, or decommissioning commands until all 5 stages complete.

* **Bare-Metal Hardware:** Configured per [Hardware Setup Guide](hardware_setup_guide.md).
* **Docker Virtual Target:** Deployed per [Virtual Environment Guide](virtual_environment_guide.md).
* **Proxmox LXC Target:** Deployed per [Virtual Environment Guide](virtual_environment_guide.md).

> [!INFO]  
> **Telemetry Stack:** Deployed automatically during `dcs-ctrl-node` bootstrapping. Refer to the [Telemetry UI Guide](telemetry_ui_guide.md) for usage details.


## 2. Service Lifecycle Management

Use these standardized utilities to inspect status, view historical or live logs, abort runs, or reset failed units across all three deployment environments:

```bash
# === Bare-Metal Hardware (executed on dcs-ctrl-node) ===
# Status: Print full status & recent logs without pagination; enable terminal wrapping
sudo systemctl status <dcs-run-baselines|dcs-run-sweeps|dcs-run-stage4> --no-pager -l
# Logs (Historical): Print full unit log history to stdout (non-blocking, wrapped)
sudo journalctl -u <dcs-run-baselines|dcs-run-sweeps|dcs-run-stage4> --no-pager -l -o cat
# Logs (Live Follow): Follow stdout/stderr streams in real time
sudo journalctl -u <dcs-run-baselines|dcs-run-sweeps|dcs-run-stage4> -f -o cat
# Abort Execution: Terminate the unit control group (sends SIGTERM, then SIGKILL)
sudo systemctl stop <dcs-run-baselines|dcs-run-sweeps|dcs-run-stage4>
# Reset Failed State: Clear transient unit failure markers
sudo systemctl reset-failed <dcs-run-baselines|dcs-run-sweeps|dcs-run-stage4>

# === Docker (executed on Docker workstation / host) ===
# Status (Host Daemon): Query container PID table directly via Docker daemon
docker top dcs-ctrl-node | grep -E "run_baselines|run_sweeps" || echo "No active test runner."
# Status (In-Container /proc): Inspect virtual filesystem via shell
docker exec dcs-ctrl-node sh -c 'grep -as "scripts/test" /proc/[0-9]*/cmdline | tr "\0" " "' || true
# Logs (Historical): Print complete execution log buffer to terminal
cat data_exports/test_execution-<baselines|sweeps|stage4>.log
# Logs (Live Follow): Tail streaming execution logs
tail -f data_exports/test_execution-<baselines|sweeps|stage4>.log
# Abort Execution: Terminate running Python runner processes
docker exec dcs-ctrl-node sh -c 'kill $(grep -l "scripts/test" /proc/[0-9]*/cmdline 2>/dev/null | cut -d/ -f3) 2>/dev/null || true'
# Reset Log Buffer: Clear log files prior to re-execution
rm -f data_exports/test_execution-<baselines|sweeps|stage4>.log

# === Proxmox LXC (executed on Proxmox VE hypervisor host) ===
# Status: Print full status & recent logs without pagination; enable terminal wrapping
pct exec 201 -- systemctl status <dcs-run-baselines|dcs-run-sweeps|dcs-run-stage4> --no-pager -l
# Logs (Historical): Print full unit log history to stdout (non-blocking, wrapped)
pct exec 201 -- journalctl -u <dcs-run-baselines|dcs-run-sweeps|dcs-run-stage4> --no-pager -l -o cat
# Logs (Live Follow): Follow stdout/stderr streams in real time
pct exec 201 -- journalctl -u <dcs-run-baselines|dcs-run-sweeps|dcs-run-stage4> -f -o cat
# Abort Execution: Terminate the unit control group
pct exec 201 -- systemctl stop <dcs-run-baselines|dcs-run-sweeps|dcs-run-stage4>
# Reset Failed State: Clear transient unit failure markers
pct exec 201 -- systemctl reset-failed <dcs-run-baselines|dcs-run-sweeps|dcs-run-stage4>

```


## 3. Execution Paths

Execute **only one** path corresponding to your target runtime environment. Stages must run sequentially (**Stage 0 $\to$ Stages 1–3 $\to$ Stage 4**).

### Path A: Bare-Metal Hardware Target (Raspberry Pi 3B)

#### Stages 0–3

**Context:** Controller Node (`dcs-ctrl-node`); Root Path: `/opt/resilient-wireless-pid`

```bash
# Stage 0: Quiet Baselines (Execute 3 consecutive runs)
# For each of the 3 runs, execute:
sudo systemctl reset-failed dcs-run-baselines 2>/dev/null || true
sudo systemd-run --unit=dcs-run-baselines --working-directory=/opt/resilient-wireless-pid \
  /opt/resilient-wireless-pid/.venv/bin/python3 scripts/test/run_baselines.py

# Wait until completion before triggering the next run. Inspect status with:
sudo systemctl status dcs-run-baselines --no-pager -l

```

```bash
# Stages 1–3: Netem Impairment Sweeps
sudo systemctl reset-failed dcs-run-sweeps 2>/dev/null || true
sudo systemd-run --unit=dcs-run-sweeps --working-directory=/opt/resilient-wireless-pid \
  /opt/resilient-wireless-pid/.venv/bin/python3 scripts/test/run_sweeps.py --duration 300

```

#### Stage 4: Adversary Cross-Traffic

Execute **four consecutive runs**, pairing one of the target loading commands on `dcs-plant-node` with `run_baselines.py` on `dcs-ctrl-node`:

* **Run 1 (Light):** `iperf3 -c 10.10.10.1 -u -b 2M -t 1200`
* **Run 2 (Medium):** `iperf3 -c 10.10.10.1 -u -b 5M -t 1200`
* **Run 3 (Heavy):** `iperf3 -c 10.10.10.1 -u -b 10M -t 1200`
* **Run 4 (Saturation):** `iperf3 -c 10.10.10.1 -u -b 0 -t 1200`

For each of the 4 loading conditions:

1. **Context:** Plant Node (`dcs-plant-node`):
```bash
# Launch cross-traffic (substitute target bandwidth: 2M, 5M, 10M, or 0)
iperf3 -c 10.10.10.1 -u -b <TARGET_RATE> -t 1200

```


2. **Context:** Controller Node (`dcs-ctrl-node` via SSH):
```bash
sudo systemctl reset-failed dcs-run-stage4 2>/dev/null || true
sudo systemd-run --unit=dcs-run-stage4 --working-directory=/opt/resilient-wireless-pid \
  /opt/resilient-wireless-pid/.venv/bin/python3 scripts/test/run_baselines.py

```


3. Once `dcs-run-stage4` completes, terminate any residual `iperf3` processes on `dcs-plant-node` before proceeding to the next run rate.

---

### Path B: Docker Virtual Target

#### Stages 0–3

**Context:** Local Docker Host; Root Path: Repository Root

```bash
# Stage 0: Quiet Baselines (Execute 3 consecutive runs)
# For each of the 3 runs, execute:
docker exec -d -w /opt/resilient-wireless-pid -e PYTHONUNBUFFERED=1 dcs-ctrl-node \
  bash -c "python3 scripts/test/run_baselines.py >> data_exports/test_execution-baselines.log 2>&1"

# Monitor progress and wait for process exit:
tail -f data_exports/test_execution-baselines.log

```

```bash
# Stages 1–3: Netem Impairment Sweeps
docker exec -d -w /opt/resilient-wireless-pid -e PYTHONUNBUFFERED=1 dcs-ctrl-node \
  bash -c "python3 scripts/test/run_sweeps.py --duration 300 >> data_exports/test_execution-sweeps.log 2>&1"

# Monitor progress:
tail -f data_exports/test_execution-sweeps.log

```

#### Stage 4: Adversary Cross-Traffic

Execute **four consecutive runs**, pairing one of the target loading commands on `dcs-plant-node` with `run_baselines.py` on `dcs-ctrl-node`:

* **Run 1 (Light):** `2M`
* **Run 2 (Medium):** `5M`
* **Run 3 (Heavy):** `10M`
* **Run 4 (Saturation):** `0` (uncapped)

For each of the 4 loading conditions:

1. **Context:** Docker Host targeting Plant Container (`dcs-plant-node`):
```bash
# Launch cross-traffic (substitute target bandwidth: 2M, 5M, 10M, or 0)
docker exec -d dcs-plant-node iperf3 -c 10.10.10.1 -u -b <TARGET_RATE> -t 1200

```


2. **Context:** Docker Host targeting Controller Container (`dcs-ctrl-node`):
```bash
docker exec -d -w /opt/resilient-wireless-pid -e PYTHONUNBUFFERED=1 dcs-ctrl-node \
  bash -c "python3 scripts/test/run_baselines.py >> data_exports/test_execution-stage4.log 2>&1"

```


3. Once the baseline execution completes, purge residual `iperf3` processes:
```bash
docker exec dcs-plant-node sh -c 'kill $(grep -l "iperf3" /proc/[0-9]*/cmdline 2>/dev/null | cut -d/ -f3) 2>/dev/null || true'
```



---

### Path C: Proxmox LXC Virtual Target

#### Stages 0–3

**Context:** Proxmox VE Host Shell

```bash
# Stage 0: Quiet Baselines (Execute 3 consecutive runs)
# For each of the 3 runs, execute:
pct exec 201 -- bash -c "systemctl reset-failed dcs-run-baselines 2>/dev/null || true \
  systemd-run --unit=dcs-run-baselines --working-directory=/opt/resilient-wireless-pid \
  /opt/resilient-wireless-pid/.venv/bin/python3 scripts/test/run_baselines.py"

# Wait until completion before triggering the next run. Inspect status with:
pct exec 201 -- systemctl status dcs-run-baselines --no-pager -l

```

```bash
# Stages 1–3: Netem Impairment Sweeps
pct exec 201 -- bash -c "systemctl reset-failed dcs-run-sweeps 2>/dev/null || true \
  systemd-run --unit=dcs-run-sweeps --working-directory=/opt/resilient-wireless-pid \
  /opt/resilient-wireless-pid/.venv/bin/python3 scripts/test/run_sweeps.py --duration 300"

```

#### Stage 4: Adversary Cross-Traffic

Execute **four consecutive runs**, pairing one of the target loading commands on container `202` (`dcs-plant-node`) with `run_baselines.py` on container `201` (`dcs-ctrl-node`):

* **Run 1 (Light):** `2M`
* **Run 2 (Medium):** `5M`
* **Run 3 (Heavy):** `10M`
* **Run 4 (Saturation):** `0` (uncapped)

For each of the 4 loading conditions:

1. **Context:** Proxmox VE Host targeting Plant Container (`202`):
```bash
# Launch cross-traffic (substitute target bandwidth: 2M, 5M, 10M, or 0)
pct exec 202 -- iperf3 -c 10.10.10.1 -u -b <TARGET_RATE> -t 1200

```


2. **Context:** Proxmox VE Host targeting Controller Container (`201`):
```bash
pct exec 201 -- bash -c "systemctl reset-failed dcs-run-stage4 2>/dev/null || true \
  systemd-run --unit=dcs-run-stage4 --working-directory=/opt/resilient-wireless-pid \
  /opt/resilient-wireless-pid/.venv/bin/python3 scripts/test/run_baselines.py"

```


3. Once `dcs-run-stage4` completes, purge residual `iperf3` processes on CT 202:
```bash
pct exec 202 -- pkill -f iperf3 || true

```



---

## 4. Post-Processing & Data Retrieval

**Context:** Local Analysis Workstation; Root Path: Repository Root

```bash
# 1. Fetch remote dataset exports (Bare-Metal Pi execution only)
./scripts/analysis/fetch_datasets.sh admin@<dcs-ctrl-node IP>

# 2. Aggregate raw InfluxDB CSV exports into partitioned metrics
python3 scripts/analysis/aggregate_and_postprocess.py --raw-dir data_exports --output-dir data_exports/processed

```

---

## 5. System Decommissioning & Teardown

**Context:** Target Controller Node / Container

```bash
# 1. Command zero-actuation failsafe via C2 (works across local and remote nodes)
./scripts/c2_ctl.sh [-t <NODE_IP>] stop

# 2. Clear Linux kernel traffic shaping qdiscs on control node
# Docker:
docker exec dcs-ctrl-node tc qdisc del dev wlan0 root 2>/dev/null || true

# Proxmox LXC:
pct exec 201 -- tc qdisc del dev wlan0 root 2>/dev/null || true

# Bare-Metal:
ssh admin@dcs-ctrl-node "sudo tc qdisc del dev wlan0 root 2>/dev/null || true"

# 3. Confirm physical fan de-energization (Bare-Metal Pi Target Only)
# ssh admin@dcs-plant-node "sudo i2cget -y 1 0x4c 0x4c"   # Must return 0x00

```

> [!INFO]
> Refer to the [Virtual Environment Guide](https://www.google.com/search?q=virtual_environment_guide.md%234-decommissioning) to clean up virtual artifacts.
