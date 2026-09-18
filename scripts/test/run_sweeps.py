#!/usr/bin/env python3
"""
scripts/test/run_sweeps.py
Automated parameter sweep and metric aggregation harness.
Orchestrates network profiles, controller architectures, and performance indices.
"""
import os
import sys
import time
import subprocess
import argparse
import logging
from dataclasses import dataclass
from typing import List, Dict, Any, Optional

import requests
import pandas as pd
import numpy as np
from influxdb_client import InfluxDBClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("SweepHarness")


@dataclass
class NetworkProfile:
    name: str
    delay_ms: float
    jitter_ms: float
    loss_pct: float
    correlation_pct: float = 0.0


class NetworkEmulationController:
    """Manages kernel queuing disciplines via POSIX iproute2 (tc)."""
    def __init__(self, interface: str):
        self.interface = interface

    def clear_rules(self) -> None:
        cmd = ["tc", "qdisc", "del", "dev", self.interface, "root"]
        subprocess.run(cmd, stdout=subprocess.DEV_NULL, stderr=subprocess.DEV_NULL)

    def apply_profile(self, profile: NetworkProfile) -> bool:
        self.clear_rules()
        if profile.delay_ms == 0 and profile.loss_pct == 0:
            logger.info("Profile '%s': Ideal line rate applied.", profile.name)
            return True

        cmd = ["tc", "qdisc", "add", "dev", self.interface, "root", "netem"]
        if profile.delay_ms > 0:
            cmd.extend(["delay", f"{profile.delay_ms}ms"])
            if profile.jitter_ms > 0:
                cmd.extend([f"{profile.jitter_ms}ms", "distribution", "normal"])
        if profile.loss_pct > 0:
            cmd.extend(["loss", f"{profile.loss_pct}%"])
            if profile.correlation_pct > 0:
                cmd.append(f"{profile.correlation_pct}%")

        logger.info("Executing: %s", " ".join(cmd))
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            logger.error("Failed to apply tc rule: %s", res.stderr.strip())
            return False
        return True


class SweepOrchestrator:
    def __init__(self, c2_url: str, influx_url: str, token: str, org: str, bucket: str, net_iface: str):
        self.c2_url = c2_url.rstrip("/")
        self.influx_client = InfluxDBClient(url=influx_url, token=token, org=org)
        self.query_api = self.influx_client.query_api()
        self.org = org
        self.bucket = bucket
        self.netem = NetworkEmulationController(interface=net_iface)

    def set_c2_state(self, payload: Dict[str, Any]) -> bool:
        try:
            res = requests.post(f"{self.c2_url}/api/control", json=payload, timeout=2.0)
            return res.status_code == 200
        except requests.RequestException as e:
            logger.error("C2 communication failure: %s", e)
            return False

    def stop_c2(self) -> bool:
        try:
            res = requests.post(f"{self.c2_url}/api/stop", timeout=2.0)
            return res.status_code == 200
        except requests.RequestException:
            return False

    def fetch_trial_metrics(self, trial_id: str) -> Optional[Dict[str, float]]:
        """Queries InfluxDB v2 via Flux and computes statistical control indices."""
        flux = f'''
        from(bucket: "{self.bucket}")
          |> range(start: -1h)
          |> filter(fn: (r) => r["_measurement"] == "control_telemetry")
          |> filter(fn: (r) => r["trial_id"] == "{trial_id}")
          |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
        '''
        try:
            df = self.query_api.query_data_frame(flux, org=self.org)
            if isinstance(df, list):
                df = pd.concat(df, ignore_index=True)
            if df.empty or "error" not in df.columns:
                logger.warning("No data points ingested for trial: %s", trial_id)
                return None

            # Calculate control performance figures of merit
            errors = df["error"].to_numpy(dtype=float)
            rtts = df["rtt_ms"].dropna().to_numpy(dtype=float) if "rtt_ms" in df.columns else np.array([])
            drops = df["packet_loss"].to_numpy(dtype=float) if "packet_loss" in df.columns else np.zeros(len(errors))

            rmse = float(np.sqrt(np.mean(errors ** 2)))
            iae = float(np.sum(np.abs(errors)))
            mean_rtt = float(np.mean(rtts)) if len(rtts) > 0 else 0.0
            drop_rate = float(np.sum(drops) / len(drops)) if len(drops) > 0 else 0.0

            return {
                "sample_count": len(errors),
                "rmse": rmse,
                "iae": iae,
                "mean_rtt_ms": mean_rtt,
                "drop_rate": drop_rate
            }
        except Exception as e:
            logger.error("Failed to query InfluxDB for trial %s: %s", trial_id, e)
            return None

    def execute_sweep(self, algorithms: List[str], profiles: List[NetworkProfile], 
                      setpoint: float, duration_s: float, output_csv: str) -> None:
        results = []
        os.makedirs(os.path.dirname(os.path.abspath(output_csv)), exist_ok=True)

        total_trials = len(algorithms) * len(profiles)
        trial_idx = 1

        logger.info("Initializing sweep: %d configurations | %.1fs per trial", total_trials, duration_s)

        for profile in profiles:
            logger.info(">>> Applying Network Condition: %s <<<", profile.name)
            if not self.netem.apply_profile(profile):
                logger.warning("Skipping profile %s due to netlink error", profile.name)
                continue

            for algo in algorithms:
                trial_id = f"sweep_{algo}_{profile.name}_{int(time.time())}"
                logger.info("[%d/%d] Starting Trial: %s (Algorithm: %s)", trial_idx, total_trials, trial_id, algo)

                # Configure and engage loop
                state_payload = {
                    "is_running": True,
                    "algorithm": algo,
                    "setpoint": setpoint,
                    "trial_id": trial_id,
                    "active_profile": profile.name
                }
                if not self.set_c2_state(state_payload):
                    logger.error("Failed to start trial %s. Halting sequence.", trial_id)
                    continue

                # Run duration window
                time.sleep(duration_s)

                # Halt actuation
                self.stop_c2()
                time.sleep(1.0)  # Drain remaining telemetry buffer

                # Extract and aggregate
                metrics = self.fetch_trial_metrics(trial_id)
                record = {
                    "trial_id": trial_id,
                    "algorithm": algo,
                    "profile_name": profile.name,
                    "delay_ms": profile.delay_ms,
                    "jitter_ms": profile.jitter_ms,
                    "loss_pct": profile.loss_pct,
                    "setpoint": setpoint,
                    "duration_s": duration_s,
                }
                if metrics:
                    record.update(metrics)
                    logger.info("Trial %s Result: RMSE=%.3f | IAE=%.2f | Mean RTT=%.2fms | Drops=%.2f%%",
                                trial_id, metrics["rmse"], metrics["iae"], metrics["mean_rtt_ms"], metrics["drop_rate"] * 100)
                else:
                    record.update({"sample_count": 0, "rmse": np.nan, "iae": np.nan, "mean_rtt_ms": np.nan, "drop_rate": np.nan})

                results.append(record)
                trial_idx += 1

                # Write incremental checkpoint to prevent data loss
                pd.DataFrame(results).to_csv(output_csv, index=False)

        # Cleanup interface
        self.netem.clear_rules()
        logger.info("Parameter sweep successfully finished. Summary persisted to %s", output_csv)


def main():
    parser = argparse.ArgumentParser(description="Automated DCS Stress-Test and Parameter Sweep Harness")
    parser.add_argument("--interface", default=os.getenv("DCS_IFACE", "wlan0"),
                        help="Network interface to attach tc netem rules")
    parser.add_argument("--duration", type=float, default=20.0, help="Duration of each step response trial in seconds")
    parser.add_argument("--setpoint", type=float, default=70.0, help="Target process variable setpoint")
    parser.add_argument("--output", default="data/sweeps/benchmark_results.csv", help="Destination CSV path")
    parser.add_argument("--c2-url", default="http://127.0.0.1:5050", help="C2 REST API Base URL")
    args = parser.parse_args()

    # Define matrix of experimental test vectors
    algorithms = ["baseline", "smith", "resilient"]
    profiles = [
        NetworkProfile(name="nominal", delay_ms=0.0, jitter_ms=0.0, loss_pct=0.0),
        NetworkProfile(name="low_jitter", delay_ms=20.0, jitter_ms=5.0, loss_pct=0.0),
        NetworkProfile(name="moderate_jitter", delay_ms=50.0, jitter_ms=15.0, loss_pct=1.0),
        NetworkProfile(name="high_jitter_loss", delay_ms=80.0, jitter_ms=30.0, loss_pct=5.0),
    ]

    orchestrator = SweepOrchestrator(
        c2_url=args.c2_url,
        influx_url=os.getenv("INFLUXDB_URL", "http://127.0.0.1:8086"),
        token=os.getenv("INFLUXDB_TOKEN", "testbed_secret_token_123"),
        org=os.getenv("INFLUXDB_ORG", "resilient_pid"),
        bucket=os.getenv("INFLUXDB_BUCKET", "wireless_pid_metrics"),
        net_iface=args.interface
    )

    try:
        orchestrator.execute_sweep(
            algorithms=algorithms,
            profiles=profiles,
            setpoint=args.setpoint,
            duration_s=args.duration,
            output_csv=args.output
        )
    except KeyboardInterrupt:
        logger.warning("Sweep terminated early by operator. Resetting qdisc.")
        orchestrator.netem.clear_rules()
        orchestrator.stop_c2()


if __name__ == "__main__":
    main()