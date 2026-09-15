### 2. `docs/development/telemetry_ui_guide.md`

```markdown
# Telemetry Web Interfaces Manual (InfluxDB v2 & Grafana)

This document specifies endpoint connectivity, automated provisioning, and operational workflows for the project's time-series observability tier. 

---

## 1. Service Endpoints & Authentication

All observability services reside on `dcs-ctrl-node` (Node 1). When running under local Docker, services bind to `localhost`. When running in Proxmox LXC (CT 201), bind to the container's external management IP (`wlan0`). 

| Service | Address (Docker) | Address (Proxmox CT 201) | Credentials | Context / Defaults |
| :--- | :--- | :--- | :--- | :--- |
| **InfluxDB v2** | `http://localhost:8086` | `http://<NODE1_IP>:8086` | `admin` / `adminpassword` | **Org:** `resilient_pid`<br>**Bucket:** `wireless_pid_metrics` |
| **Grafana** | `http://localhost:3000` | `http://<NODE1_IP>:3000` | `admin` / `admin` | Auto-provisioned from `configs/grafana/` |
| **C2 Console** | `http://localhost:5050` | `http://<NODE1_IP>:5050` | None (Open REST/UI) | Supervisory control and state store |

---

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

---

## 3. InfluxDB Operations (Ad-Hoc Querying)

Use InfluxDB directly for fast data verification, raw metric inspection, and debugging packet ingestion.

1. **Access Explorer**: Log in at port `8086`, then select **Data Explorer** (graph icon on the left rail). 
2. **Query Builder**: 
    * **Bucket**: Select `wireless_pid_metrics`. 
    * **_measurement**: Select `control_telemetry`. 
    * **_field**: Select `process_variable`, `setpoint`, and `control_effort` (or `control_signal`). 
3. **Inspect Output**: Select a target window (e.g., `Past 5m`) and click **Submit**. 
4. **Script Mode (Flux)**: Click **Script Editor** to query specific experimental trial IDs: 

```flux
from(bucket: "wireless_pid_metrics")
  |> range(start: -5m)
  |> filter(fn: (r) => r["_measurement"] == "control_telemetry")
  |> filter(fn: (r) => r.trial_id =~ /eval_trial/)
  |> yield(name: "trial_filtered")

```

---

## 4. Grafana Operations

Grafana is pre-configured via declarative provisioning files located in `configs/grafana/`: 

* **Datasources:** Linked automatically to InfluxDB via `configs/grafana/provisioning/datasources/influxdb.yaml`. 
* **Dashboards:** Provisioned automatically from `configs/grafana/dashboards/` on container boot. 

### 4.1 Viewing Dashboards

1. Log in at port `3000` (`admin` / `admin`). 
2. Select **Dashboards** in the left sidebar. 
3. Open the **Control Systems** folder and select the **Wireless PID Telemetry Dashboard**. 
4. Set the top-right time window to **Last 1 minute** and set auto-refresh to **1s**. 


### 4.2 Persisting Dashboard Updates to Git

If you modify dashboard panels or threshold configurations in the UI, export your changes to version control: 

1. Click **Dashboard Settings** (gear icon in the top toolbar). 
2. Click **JSON Model** in the left settings menu. 
3. Copy the JSON object and overwrite `configs/grafana/dashboards/dcs_pid_dashboard.json`. 
4. Commit the updated JSON schema to Git. 
