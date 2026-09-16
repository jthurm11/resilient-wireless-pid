# Telemetry Web Interfaces Manual (InfluxDB v2 & Grafana)

This document details baseline access, verification, and manual dashboard operations for the project telemetry layer.


## 1. Service Endpoints & Authentication

All observability services reside on `dcs-ctrl-node`. Replace `<NODE1_IP>` with your node's routable management IP or use `127.0.0.1` locally. 

| Service | URL | Credentials | Context / Params |
| :--- | :--- | :--- | :--- |
| **InfluxDB v2** | `http://127.0.0.1:8086` | `admin` / `adminpassword` | **Org:** `resilient_pid`<br>**Bucket:** `wireless_pid_metrics` |
| **Grafana** | `http://127.0.0.1:3000` | `admin` / `admin` | Auto-provisions from `configs/grafana/` |
| **C2 Console** | `http://127.0.0.1:5050` | None (Open REST/UI) | Supervisory control and state store |


## 2. Infrastructure Automation (`telemetry_stack.sh`)

When running inside Proxmox CT 201 or directly on bare-metal hardware, the telemetry services are managed through `scripts/infra/telemetry_stack.sh` (which wraps `deploy/docker/telemetry-compose.yml`).

```bash
# Provision or update InfluxDB v2 and Grafana
./scripts/infra/telemetry_stack.sh create

# Inspect container health and exposed ports
./scripts/infra/telemetry_stack.sh status

# Stop containers and purge volumes
./scripts/infra/telemetry_stack.sh destroy

# Optional: Run quietly without ASCII headers
./scripts/infra/telemetry_stack.sh status --no-header

```


## 3. InfluxDB Operations (Ad-Hoc Querying)

Use InfluxDB directly for rapid ingest validation and raw time-series inspection.

1. ****Access Explorer**: Navigate to `http://127.0.0.1:8086` and select **Data Explorer** (graph icon on the left rail). 
2. **Query Builder**: 
    * **Bucket**: Select `wireless_pid_metrics`. 
    * **_measurement**: Select `control_telemetry`. 
    * **_field**: Select `process_variable`, `setpoint`, and `control_effort`. 
3. **Inspect Output**: Select a target window (e.g., `Past 5m`) and click **Submit**. 
4. **Script Mode (Flux)**: Click **Script Editor** to query specific experimental trial IDs: 

```flux
from(bucket: "wireless_pid_metrics")
  |> range(start: -5m)
  |> filter(fn: (r) => r["_measurement"] == "control_telemetry")
  |> filter(fn: (r) => r.trial_id == "smoke_test_123")
  |> yield(name: "trial_filtered")

```


## 4. Grafana Operations

Grafana is pre-configured via declarative provisioning files located in `configs/grafana/`: 

### 4.1 Viewing Dashboards

1. Log in at `http://127.0.0.1:3000` (`admin` / `admin`).
2. Select **Dashboards** in the left sidebar. 
3. Open the **Control Systems** folder and select the **Wireless PID Telemetry Dashboard**. 
4. Set the top-right time window to **Last 1 minute** and set auto-refresh to **1s**. 

### 4.2 Dashboard Model Synchronization

To persist modifications made in the Grafana UI back to source control:

1. Click **Dashboard Settings** (gear icon in the top toolbar). 
2. Click **JSON Model** in the left settings menu. 
3. Copy the JSON object and overwrite `configs/grafana/dashboards/dcs_pid_dashboard.json`. 
4. Commit the updated JSON schema to Git. 
