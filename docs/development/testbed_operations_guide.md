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
| **Stage 4** | Adversary Cross-Traffic | `iperf3` + `run_baselines.py` | Unshaped link + UDP cross-traffic ($200, 400, 600, 800\text{ Kbps}$) | 12 cross-traffic saturation trials logged ($N=4$ per algorithm). | 

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


## 2. Execution Paths

Execute **only one** path corresponding to your target runtime environment. Experimental stages must execute sequentially: **Stage 0 $\to$ Stages 1–3 $\to$ Stage 4**.

### Path A: Bare-Metal Hardware Target (Raspberry Pi 3B)

#### Lifecycle & Process Management Utilities
**Host Context:** Controller Node (`admin@dcs-ctrl-node`); Root Directory: `/opt/resilient-wireless-pid`

| Purpose | Command | Description |
| :--- | :--- | :--- |
| **Status** | `sudo systemctl status <UNIT> --no-pager -l` | Query transient unit lifecycle state, exit code, and recent runtime log buffer without pagination. |
| **Historical Logs** | `sudo journalctl -u <UNIT> --no-pager -l -o cat` | Dump unpaginated unit stdout/stderr history directly to terminal standard output. |
| **Live Follow** | `sudo journalctl -u <UNIT> -f -o cat` | Follow stdout/stderr streams in real time via journald socket forwarder. |
| **Abort Run** | `sudo systemctl stop <UNIT>` | Terminate unit control group atomically; propagates `SIGTERM`, followed by `SIGKILL` if unstopped. |
| **Reset Unit** | `sudo systemctl reset-failed <UNIT>` | Clear systemd unit error state flags and transient execution counters prior to re-execution. |

> *Valid `<UNIT>` targets:* `dcs-run-baselines`, `dcs-run-sweeps`, `dcs-run-stage4`, `dcs-iperf`

#### Stages 0–3: Baselines & Impairment Sweeps

```bash
# Stage 0: Quiet Baselines (Execute 3 consecutive runs)
# For each run (Run 1 to 3):
sudo systemctl reset-failed dcs-run-baselines 2>/dev/null || true
sudo systemd-run --unit=dcs-run-baselines --working-directory=/opt/resilient-wireless-pid \
  /opt/resilient-wireless-pid/.venv/bin/python3 scripts/test/run_baselines.py

# Await completion before triggering the next run. Inspect status with:
sudo systemctl status dcs-run-baselines --no-pager -l

> [!CAUTION]  
> Do NOT execute any teardown, cleanup, or decommissioning commands until all 5 stages complete.

```bash
# Stages 1–3: Netem Impairment Sweeps (Delay, Jitter, Packet Loss)
sudo systemctl reset-failed dcs-run-sweeps 2>/dev/null || true
sudo systemd-run --unit=dcs-run-sweeps --working-directory=/opt/resilient-wireless-pid \
  /opt/resilient-wireless-pid/.venv/bin/python3 scripts/test/run_sweeps.py --duration 300

# Stream execution progress:
sudo journalctl -u dcs-run-sweeps -f -o cat

```

#### Stage 4: Adversary Cross-Traffic

Execute **four consecutive runs**, pairing synthetic transport loading on `dcs-plant-node` with the telemetry test orchestrator on `dcs-ctrl-node`:

For each of the 4 loading conditions:

1. **Plant Node (`dcs-plant-node`):** Launch isolated background UDP generator

```bash
# Substitute <TARGET_RATE> with 200K, 400K, 600K, or 800K
sudo systemctl reset-failed dcs-iperf 2>/dev/null || true
sudo systemd-run --unit=dcs-iperf --property="CPUAffinity=3" --property="Nice=10" --property="KillMode=control-group" \
  iperf3 -c 10.10.10.1 -u -b <TARGET_RATE> -t 1200

```

2. **Controller Node (`dcs-ctrl-node`):** Launch trial orchestrator with rate tracking metadata

```bash
# Match <RATE_KBPS> to active cross-traffic (200, 400, 600, or 800)
sudo systemctl reset-failed dcs-run-stage4 2>/dev/null || true
sudo systemd-run --unit=dcs-run-stage4 --working-directory=/opt/resilient-wireless-pid \
  /opt/resilient-wireless-pid/.venv/bin/python3 scripts/test/run_baselines.py --adversary-rate <RATE_KBPS>

# Monitor active trial progress:
sudo journalctl -u dcs-run-stage4 -f -o cat

```

3. **Plant Node (`dcs-plant-node`):** Post-run cgroup termination and socket buffer drain

```bash
# Terminate cross-traffic unit and enforce a 15-second quiet window before next run
sudo systemctl stop dcs-iperf 2>/dev/null || true
sudo systemctl reset-failed dcs-iperf 2>/dev/null && sleep 15

### Path A: Bare-Metal Hardware Target (Raspberry Pi 3B)

---

### Path B: Docker Virtual Target

#### Lifecycle & Process Management Utilities
**Host Context:** Local Workstation / Docker Engine Shell; Root Directory: Repository Root

| Purpose | Command | Description |
| :--- | :--- | :--- |
| **Status (Daemon)** | `docker top dcs-ctrl-node \| grep -E "python3" \|\| echo "Inactive"` | Inspect active containerized Python trial runners via the host daemon process table. |
| **Status (Adversary)** | `docker ps --filter "name=dcs-iperf"` | Interrogate active ephemeral adversary sidecar containers (server and client). |
| **Historical Logs** | `cat data_exports/test_execution-<STAGE>.log` | Print captured trial telemetry and stdout/stderr history from the host runtime volume. |
| **Live Follow** | `tail -f data_exports/test_execution-<STAGE>.log` | Follow live orchestrator execution progress and InfluxDB export telemetry. |
| **Abort Orchestrator** | `docker exec dcs-ctrl-node pkill -f "scripts/test"` | Terminate running Python sweep/baseline runners inside the controller container. |
| **Abort Adversary** | `docker stop dcs-iperf-client dcs-iperf-server 2>/dev/null \|\| true` | Atomically tear down ephemeral sidecar containers and release bound container ports. |
| **Reset Buffer** | `rm -f data_exports/test_execution-<STAGE>.log` | Purge residual log buffers prior to re-executing trial vectors. |

> *Valid `<STAGE>` suffixes:* `baselines`, `sweeps`, `stage4`

#### Stages 0–3: Baselines & Impairment Sweeps
**Context:** Local Docker Host targeting container `dcs-ctrl-node`

```bash
# Stage 0: Quiet Baselines (Execute 3 consecutive runs)
# For each run (Run 1 to 3):
rm -f data_exports/test_execution-baselines.log
docker exec -d -w /opt/resilient-wireless-pid -e PYTHONUNBUFFERED=1 dcs-ctrl-node \
  bash -c "python3 scripts/test/run_baselines.py >> data_exports/test_execution-baselines.log 2>&1"

# Await completion before triggering next run. Follow progress via log:
tail -f data_exports/test_execution-baselines.log

# Stages 1–3: Netem Impairment Sweeps
sudo systemd-run --unit=dcs-run-sweeps --working-directory=/opt/resilient-wireless-pid /opt/resilient-wireless-pid/.venv/bin/python3 scripts/test/run_sweeps.py
```

```bash
# Stages 1–3: Netem Impairment Sweeps (Delay, Jitter, Packet Loss)
rm -f data_exports/test_execution-sweeps.log
docker exec -d -w /opt/resilient-wireless-pid -e PYTHONUNBUFFERED=1 dcs-ctrl-node \
  bash -c "python3 scripts/test/run_sweeps.py --duration 300 >> data_exports/test_execution-sweeps.log 2>&1"

# Stream execution progress:
tail -f data_exports/test_execution-sweeps.log

```

#### Stage 4: Adversary Cross-Traffic

Execute **four consecutive runs**, pairing synthetic transport loading on `dcs-plant-node` with the telemetry test orchestrator on `dcs-ctrl-node`. Adversary traffic is injected via lightweight ephemeral sidecar containers attached directly to the container network namespaces (`--net=container:<target>`):

> [!NOTE]  
> On the Docker virtual bridge, memory bus throughput clears UDP frames with near-zero loss (< 1%). This step validates containerized orchestration parity, telemetry tagging, and data pipeline conformance with bare-metal executions. 

**Pre-flight (Controller Ingress):** Initialize ephemeral listening daemon attached to controller container network stack:

```bash
docker rm -f dcs-iperf-server 2>/dev/null || true
docker run -d --name dcs-iperf-server \
  --net=container:dcs-ctrl-node \
  --rm networkstatic/iperf3 -s -B 0.0.0.0

```

For each of the 4 loading conditions:

1. **Host Terminal targeting Plant Network Stack:** Launch ephemeral UDP cross-traffic generator

```bash
# Clean up stale client container and run new adversary burst
docker rm -f dcs-iperf-client 2>/dev/null || true
# Substitute <TARGET_RATE> with 200K, 400K, 600K, or 800K
docker run -d --name dcs-iperf-client \
  --net=container:dcs-plant-node \
  --rm networkstatic/iperf3 -c 10.10.10.1 -u -b <TARGET_RATE> -t 1200

#### Stages 0-3 

2. **Host Terminal targeting Controller Container (`dcs-ctrl-node`):** Launch trial orchestrator

```bash
# Match <RATE_KBPS> to active cross-traffic (200, 400, 600, or 800)
rm -f data_exports/test_execution-stage4.log
docker exec -d -w /opt/resilient-wireless-pid -e PYTHONUNBUFFERED=1 dcs-ctrl-node \
  bash -c "python3 scripts/test/run_baselines.py --adversary-rate <RATE_KBPS> >> data_exports/test_execution-stage4.log 2>&1"

# Stream progress until completion:
tail -f data_exports/test_execution-stage4.log

# Stages 1–3: Netem Impairment Sweeps
docker exec -d -w /opt/resilient-wireless-pid dcs-ctrl-node python3 scripts/test/run_sweeps.py >> data_exports/test_execution-sweeps.log 2>&1
```

3. **Host Terminal:** Purge ephemeral adversary client and enforce socket drain window

```bash
# Terminate ephemeral client container atomically (SIGTERM/SIGKILL)
docker stop dcs-iperf-client 2>/dev/null || true

# Hold 15s to clear virtual bridge buffers and InfluxDB flush pipelines before next rate
sleep 15

```

Repeat the previous steps for all loading conditions before progressing. 

**Post-flight (Controller Ingress):** Terminate listener daemon once all Stage 4 runs conclude:

```bash
docker stop dcs-iperf-server 2>/dev/null || true

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

#### Lifecycle & Process Management Utilities

**Host Context:** Proxmox VE Hypervisor Shell; Managed Containers: CT `201` (`dcs-ctrl-node`), CT `202` (`dcs-plant-node`)

| Purpose | Command | Description |
| --- | --- | --- |
| **Status** | `pct exec <CTID> -- systemctl status <UNIT> --no-pager -l` | Query transient unit status, task PID, and logs inside target container. |
| **Historical Logs** | `pct exec <CTID> -- journalctl -u <UNIT> --no-pager -l -o cat` | Inspect historical stdout/stderr execution log from target container journal. |
| **Live Follow** | `pct exec <CTID> -- journalctl -u <UNIT> -f -o cat` | Follow live container unit stdout/stderr logging stream from the hypervisor. |
| **Abort Run** | `pct exec <CTID> -- systemctl stop <UNIT>` | Terminate the containerized systemd unit and tear down the control group. |
| **Reset Unit** | `pct exec <CTID> -- systemctl reset-failed <UNIT>` | Clear systemd unit failure flags inside container environment. |

> *Valid `<CTID>` targets:* `201` (Controller), `202` (Plant)
> *Valid `<UNIT>` targets:* `dcs-run-baselines`, `dcs-run-sweeps`, `dcs-run-stage4`, `dcs-iperf`

#### Stages 0–3: Baselines & Impairment Sweeps

**Context:** Proxmox VE Host Shell targeting Controller Container (`CT 201`)

```bash
# Stage 0: Quiet Baselines (Execute 3 consecutive runs)
# For each run (Run 1 to 3):
pct exec 201 -- systemctl reset-failed dcs-run-baselines 2>/dev/null || true
pct exec 201 -- systemd-run --unit=dcs-run-baselines --working-directory=/opt/resilient-wireless-pid \
  /opt/resilient-wireless-pid/.venv/bin/python3 scripts/test/run_baselines.py

# Await completion before triggering next run. Inspect status with:
pct exec 201 -- systemctl status dcs-run-baselines --no-pager -l

```

```bash
# Stages 1–3: Netem Impairment Sweeps (Delay, Jitter, Packet Loss)
pct exec 201 -- systemctl reset-failed dcs-run-sweeps 2>/dev/null || true
pct exec 201 -- systemd-run --unit=dcs-run-sweeps --working-directory=/opt/resilient-wireless-pid \
  /opt/resilient-wireless-pid/.venv/bin/python3 scripts/test/run_sweeps.py --duration 300

# Stream execution progress:
pct exec 201 -- journalctl -u dcs-run-sweeps -f -o cat

# Stages 1–3: Netem Impairment Sweeps
pct exec 201 -- systemd-run --unit=dcs-run-sweeps --working-directory=/opt/resilient-wireless-pid /opt/resilient-wireless-pid/.venv/bin/python3 scripts/test/run_sweeps.py
```

#### Stage 4: Adversary Cross-Traffic

Execute **four consecutive runs**, pairing synthetic transport loading on CT `202` (`dcs-plant-node`) with the telemetry test orchestrator on CT `201` (`dcs-ctrl-node`):

For each of the 4 loading conditions:

1. **Proxmox Host targeting Plant Container (`CT 202`):** Launch isolated background UDP generator

```bash
# Substitute <TARGET_RATE> with 200K, 400K, 600K, or 800K
pct exec 202 -- systemctl reset-failed dcs-iperf 2>/dev/null || true
pct exec 202 -- systemd-run --unit=dcs-iperf --property="CPUAffinity=3" --property="Nice=10" --property="KillMode=control-group" \
  iperf3 -c 10.10.10.1 -u -b <TARGET_RATE> -t 1200

## 4. Post-Processing & Data Retrieval

2. **Proxmox Host targeting Controller Container (`CT 201`):** Launch trial orchestrator with rate tracking metadata

```bash
# Match <RATE_KBPS> to active cross-traffic (200, 400, 600, or 800)
pct exec 201 -- systemctl reset-failed dcs-run-stage4 2>/dev/null || true
pct exec 201 -- systemd-run --unit=dcs-run-stage4 --working-directory=/opt/resilient-wireless-pid \
  /opt/resilient-wireless-pid/.venv/bin/python3 scripts/test/run_baselines.py --adversary-rate <RATE_KBPS>

# Monitor active trial progress:
pct exec 201 -- journalctl -u dcs-run-stage4 -f -o cat

```

3. **Proxmox Host targeting Plant Container (`CT 202`):** Post-run cgroup termination and socket buffer drain

```bash
# Terminate cross-traffic unit and enforce a 15-second quiet window before next run
pct exec 202 -- systemctl stop dcs-iperf 2>/dev/null || true
pct exec 202 -- systemctl reset-failed dcs-iperf 2>/dev/null && sleep 15

# 2. Aggregate raw InfluxDB CSV exports into partitioned metrics
python3 scripts/analysis/aggregate_and_postprocess.py --raw-dir data_exports --output-dir data_exports/processed
```

---

## 4. Post-Processing & Data Retrieval

**Context:** Local Analysis Workstation; Root Path: Repository Root

```bash
# 1. Fetch remote dataset exports (Bare-Metal Pi execution only)
./scripts/analysis/fetch_datasets.sh rpi <dcs-ctrl-node IP>

# 2. Aggregate raw InfluxDB CSV exports into partitioned metrics
python3 scripts/analysis/aggregate_and_postprocess.py --raw-dir data_exports --output-dir data_exports/processed \
  --environment <hardware_rpi|docker|proxmox_lxc>

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
