#!/usr/bin/env python3
"""
scripts/test_telemetry_pipeline.py
Synthetic smoke test to validate InfluxDB v2 line protocol ingestion.
"""
import os
import time
import math
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

INFLUX_URL = os.getenv("INFLUXDB_URL", "http://localhost:8086")
INFLUX_TOKEN = os.getenv("INFLUXDB_TOKEN", "testbed_secret_token_123")
INFLUX_ORG = os.getenv("INFLUXDB_ORG", "resilient_pid")
INFLUX_BUCKET = os.getenv("INFLUXDB_BUCKET", "wireless_pid_metrics")

print(f"Connecting to InfluxDB at {INFLUX_URL} (Org: {INFLUX_ORG}, Bucket: {INFLUX_BUCKET})...")

client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
write_api = client.write_api(write_options=SYNCHRONOUS)

trial_id = f"smoke_test_{int(time.time())}"
setpoint = 100.0
dt = 0.02  # 50 Hz
steps = 500

print(f"Streaming {steps} synthetic steps for {trial_id}...")

for k in range(steps):
    t_start = time.perf_counter()
    t = k * dt
    
    # Synthetic damped response: y(t) = SP * (1 - e^(-1.5t) * cos(3t))
    pv = setpoint * (1.0 - math.exp(-1.5 * t) * math.cos(3.0 * t))
    error = setpoint - pv
    u_t = 1.2 * error + 0.05 * math.sin(10.0 * t)  # Synthetic control effort
    rtt = 0.005 + 0.002 * math.sin(5.0 * t)        # Synthetic RTT

    point = (
        Point("control_telemetry")
        .tag("trial_id", trial_id)
        .tag("algorithm", "synthetic_validation")
        .field("setpoint", float(setpoint))
        .field("process_variable", float(pv))
        .field("control_effort", float(u_t))
        .field("error", float(error))
        .field("rtt", float(rtt))
        .time(time.time_ns(), WritePrecision.NS)
    )
    
    write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=point)
    
    elapsed = time.perf_counter() - t_start
    sleep_time = max(0.0, dt - elapsed)
    time.sleep(sleep_time)

client.close()
print("Synthetic ingestion complete. InfluxDB populated.")