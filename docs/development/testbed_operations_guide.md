# Testbed Operations & Trial Execution Procedure

This document defines repeatable test execution steps for running the **84 standardized trial scenarios** across the three core control architectures:
- **`discrete`**: Standard Discrete PID (`DiscretePID`) with derivative filtering ($\tau_f = 0.35\text{ s}$) and anti-windup clamping.
- **`smith`**: Smith Predictor dead-time compensator (`SmithPredictor`) with 2nd-order discrete ARX plant model and circular delay buffer.
- **`resilient`**: Resilient Predictive Observer PID (`ResilientPID`) with 1-step recursive ARX prediction and integral freezing ($\Delta I = 0$) during packet dropouts ($is\_loss = True$).

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
>  


## 2. Service Lifecycle Management

Use these standardized utilities to inspect status, abort, or reset transient test services across environments:

```bash
# === Bare-Metal Hardware (executed on dcs-ctrl-node) ===
sudo systemctl status <dcs-run-baselines|dcs-run-sweeps>           # Status
sudo journalctl -u <dcs-run-baselines|dcs-run-sweeps> -f -o cat    # Logs
sudo systemctl stop <dcs-run-baselines|dcs-run-sweeps>             # Abort execution
sudo systemctl reset-failed                                        # Reset failed state

# === Docker (executed on Docker host) ===
tail -f data_exports/test_execution-<baselines|sweeps|stage4>.log  # Stream logs (written locally to Docker host)
docker top dcs-ctrl-node | grep -E "run_baselines|run_sweeps"      # Find process ID to terminate execution `pkill <PID>`

# === Proxmox LXC (executed on Proxmox VE host) ===
pct exec 201 -- systemctl status <dcs-run-baselines|dcs-run-sweeps> 
pct exec 201 -- journalctl -u <dcs-run-baselines|dcs-run-sweeps> -f -o cat 
pct exec 201 -- systemctl stop <dcs-run-baselines|dcs-run-sweeps>
pct exec 201 -- systemctl reset-failed
```


## 3. Execution Paths

Execute **only one** path corresponding to your target runtime environment. Stages must run sequentially (**Stage 0 $\to$ Stages 1–3 $\to$ Stage 4**).

### Path A: Bare-Metal Hardware Target (Raspberry Pi 3B)

#### Stages 0-3 

**Context:** Controller Node (`dcs-ctrl-node`); Root Path: `/opt/resilient-wireless-pid`

```bash
# Stage 0: Quiet Baselines (Perform 3 consecutive runs)
sudo systemd-run --unit=dcs-run-baselines --working-directory=/opt/resilient-wireless-pid /opt/resilient-wireless-pid/.venv/bin/python3 scripts/test/run_baselines.py

# Wait until service completes, then run:
systemctl reset-failed dcs-baseline-runner 2>/dev/null || true
```
```bash
# Stages 1–3: Netem Impairment Sweeps
sudo systemd-run --unit=dcs-run-sweeps --working-directory=/opt/resilient-wireless-pid /opt/resilient-wireless-pid/.venv/bin/python3 scripts/test/run_sweeps.py
```

#### Stage 4 (Adversary Cross-Traffic)

Perform four consecutive runs of the below steps to accomplish baseline trails against these target rates: 
1. Light Loading: 2 Mbps (`iperf3 -c 10.10.10.1 -u -b 2M -t 1200`) 
2. Medium Loading: 5 Mbps (`iperf3 -c 10.10.10.1 -u -b 5M -t 1200`) 
3. Heavy Loading: 10 Mbps (`iperf3 -c 10.10.10.1 -u -b 10M -t 1200`) 
4. Channel Saturation: Uncapped / Maximum Link Rate (`iperf3 -c 10.10.10.1 -u -b 0 -t 1200`)


Execute the following steps in tandem, substituting the `iperf3` command for desired target rate on each consecutive run: 

1. **Context:** Plant Node (`dcs-plant-node` or secondary traffic generator node):
   ```bash
   # Rate 1 - Light Loading (2Mbps)
   iperf3 -c 10.10.10.1 -u -b 2M -t 1200
   ```
2. **Context:** Controller Node (`dcs-ctrl-node`); Root Path: `/opt/resilient-wireless-pid`:
   ```bash
   sudo systemd-run --unit=dcs-run-stage4 --working-directory=/opt/resilient-wireless-pid /opt/resilient-wireless-pid/.venv/bin/python3 scripts/test/run_baselines.py
   ```
Once `run_baselines.py` completes, reset the systemd unit before the next execution: 
```bash
sudo systemctl reset-failed dcs-run-stage4
```

---

### Path B: Docker Virtual Target

#### Stages 0-3 

**Context:** Local Docker Host, Root Path: Repository Root

```bash
# Stage 0: Quiet Baselines (Perform 3 consecutive runs)
docker exec -d -w /opt/resilient-wireless-pid -e PYTHONUNBUFFERED=1 dcs-ctrl-node \
  bash -c "python3 scripts/test/run_baselines.py >> data_exports/test_execution-baselines.log 2>&1" 
```
```bash
# Stages 1–3: Netem Impairment Sweeps
docker exec -d -w /opt/resilient-wireless-pid -e PYTHONUNBUFFERED=1 dcs-ctrl-node \
  bash -c "python3 scripts/test/run_sweeps.py >> data_exports/test_execution-sweeps.log 2>&1" 
```

#### Stage 4 (Adversary Cross-Traffic)

Execute the following steps in tandem: 

1. **Context:** Docker Host targetting Plant Container (`dcs-plant-node`):
   ```bash
   docker exec -d dcs-plant-node iperf3 -c 10.10.10.1 -u -b 10M -t 1200
   ```
2. **Context:** Docker Host targetting Controller Container (`dcs-ctrl-node`):
   ```bash
   docker exec -d -w /opt/resilient-wireless-pid dcs-ctrl-node python3 scripts/test/run_baselines.py >> data_exports/test_execution-stage4.log 2>&1
   ```

---

### Path C: Proxmox LXC Virtual Target

#### Stages 0-3 

**Context:** Proxmox VE Host; Root Path: Host Shell

```bash
# Stage 0: Quiet Baselines (Perform 3 consecutive runs)
pct exec 201 -- systemd-run --unit=dcs-run-baselines --working-directory=/opt/resilient-wireless-pid /opt/resilient-wireless-pid/.venv/bin/python3 scripts/test/run_baselines.py

# Wait until service completes, then run:
pct exec 201 -- systemctl reset-failed dcs-baseline-runner 2>/dev/null || true
```
```bash
# Stages 1–3: Netem Impairment Sweeps
pct exec 201 -- systemd-run --unit=dcs-run-sweeps --working-directory=/opt/resilient-wireless-pid /opt/resilient-wireless-pid/.venv/bin/python3 scripts/test/run_sweeps.py
```

#### Stage 4 (Adversary Cross-Traffic) 

Execute the following steps in tandem: 

1. **Context:** Proxmox VE Host targetting Plant Container (`202`):
   ```bash
   pct exec 202 -- iperf3 -c 10.10.10.1 -u -b 10M -t 1200
   ```
2. **Context:** Proxmox VE Host targetting Controller Container (`201`):
   ```bash
   pct exec 201 -- systemd-run --unit=dcs-run-baselines --working-directory=/opt/resilient-wireless-pid /opt/resilient-wireless-pid/.venv/bin/python3 scripts/test/run_baselines.py
   ```

---

## 4. Post-Processing & Data Retrieval

**Context:** Local Analysis Workstation, Root Path: Repository Root

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
> Refer to the [Virtual Environment Guide](virtual_environment_guide.md#4-decommissioning) to clean up virtual artifacts. 

