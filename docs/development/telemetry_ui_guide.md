# Telemetry Web Interfaces Manual (InfluxDB v2 & Grafana)

This document details baseline access, verification, and manual dashboard operations for the project telemetry layer.

---

## 1. Service Endpoints & Authentication

All services execute on the controller/historian node. Replace `<NODE1_IP>` with your node's routable management IP.

| Service | URL | Credentials | Context / Params |
| :--- | :--- | :--- | :--- |
| **InfluxDB v2** | `http://<NODE1_IP>:8086` | `admin` / `adminpassword` | **Org:** `resilient_pid`<br>**Bucket:** `wireless_pid_metrics` |
| **Grafana** | `http://<NODE1_IP>:3000` | `admin` / `admin` | Auto-provisions from `configs/grafana/` |

---

## 2. InfluxDB Operations (Ad-Hoc Querying)

Use InfluxDB directly for rapid ingest validation and raw time-series inspection.

1. **Access Explorer**: Navigate to `http://<NODE1_IP>:8086` and select **Data Explorer** (graph icon on the left rail).
2. **Query Builder**:
   * **Bucket**: Select `wireless_pid_metrics`.
   * **_measurement**: Select `control_telemetry`.
   * **_field**: Check `process_variable`, `setpoint`, and `control_effort`.
3. **Inspect Output**: Choose a target inspection window (e.g., `Past 5m`) and click **Submit**.
4. **Script Mode (Flux)**: Click **Script Editor** to isolate metrics by specific experimental trial runs:
   ```flux
   from(bucket: "wireless_pid_metrics")
     |> range(start: -5m)
     |> filter(fn: (r) => r["_measurement"] == "control_telemetry")
     |> filter(fn: (r) => r["trial_id"] =~ /smoke_test/)
     |> yield(name: "trial_filtered")

    ```

---

## 3. Grafana Operations

Grafana automatically links the InfluxDB datasource and imports dashboard models on service initialization via `configs/grafana/`.

### 3.1 Viewing Provisioned Dashboards

1. Log in at `http://<NODE1_IP>:3000` (`admin` / `admin`).
2. Navigate to **Dashboards** in the left navigation menu.
3. Open the **Control Systems** folder and select **Wireless PID Telemetry Dashboard**.
4. Set the top-right time picker to **Last 1 minute** and configure the refresh interval to **1s**.

### 3.2 Dashboard Model Synchronization

To persist modifications made in the Grafana UI back to source control:

* **Export Dashboard to Repository**:
    1. Open the active dashboard view.
    2. Click **Dashboard Settings** (gear icon in the top utility bar).
    3. Select **JSON Model** in the left navigation rail.
    4. Copy the complete JSON structure and overwrite `configs/grafana/dashboards/dcs_pid_dashboard.json`.
    5. Commit the modified template to Git.


* **Manual Import into an Ad-Hoc Instance**:
    1. Navigate to **Dashboards > New > Import**.
    2. Upload the exported `.json` file or paste the raw JSON schema directly into the panel.
    3. Map the target InfluxDB datasource dropdown to `InfluxDB_Flux` and select **Import**. 

