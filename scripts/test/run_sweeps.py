#!/usr/bin/env python3
"""
scripts/test/run_sweeps.py
Automated parameter sweep orchestrator and telemetry snapshot pipeline.
Coordinates tc/netem kernel qdiscs, C2 REST states, and standardized InfluxDB CSV exports.
"""

import argparse
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import List

import pandas as pd
import requests
from influxdb_client import InfluxDBClient

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("SweepOrchestrator")


@dataclass
class TrialConfig:
    """Deterministic configuration structure for an isolated parameter sweep trial."""

    trial_id: str
    algorithm: str
    setpoint: float
    netem_args: List[str]
    duration_sec: float
    export_filename: str


class TrafficController:
    """Manages Linux kernel tc/netem queuing disciplines via iproute2."""

    def __init__(self, interface: str):
        self.interface = interface

    def clear_rules(self) -> None:
        """Purge root qdisc to restore clean, unshaped line-rate conditions."""
        cmd = ["tc", "qdisc", "del", "dev", self.interface, "root"]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0 and "No such file or directory" not in res.stderr:
            logger.debug(
                "tc cleanup notice (%s): %s", self.interface, res.stderr.strip()
            )

    def apply_netem(self, netem_args: List[str]) -> None:
        """Attach configured netem qdisc rules to root egress."""
        self.clear_rules()
        cmd = [
            "tc",
            "qdisc",
            "add",
            "dev",
            self.interface,
            "root",
            "netem",
        ] + netem_args
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            logger.error(
                "Failed executing tc rule '%s': %s", " ".join(cmd), res.stderr.strip()
            )
            raise RuntimeError(f"tc execution failed: {res.stderr.strip()}")
        logger.info("Kernel netem applied: %s", " ".join(netem_args))


class SweepOrchestrator:
    """Coordinates empirical impairment sweeps across C2, netem, and InfluxDB."""

    def __init__(
        self,
        interface: str,
        c2_url: str = "http://127.0.0.1:5050",
        influx_url: str = os.getenv("INFLUXDB_URL", "http://127.0.0.1:8086"),
        influx_token: str = os.getenv("INFLUXDB_TOKEN", "testbed_secret_token_123"),
        influx_org: str = os.getenv("INFLUXDB_ORG", "resilient_pid"),
        influx_bucket: str = os.getenv("INFLUXDB_BUCKET", "wireless_pid_metrics"),
        data_export_dir: str = "/opt/resilient-wireless-pid/data_exports",
    ):
        self.tc = TrafficController(interface)
        self.c2_url = c2_url.rstrip("/")
        self.influx_url = influx_url
        self.influx_token = influx_token
        self.influx_org = influx_org
        self.influx_bucket = influx_bucket
        self.data_export_dir = data_export_dir

        os.makedirs(self.data_export_dir, exist_ok=True)
        self.influx_client = InfluxDBClient(
            url=self.influx_url,
            token=self.influx_token,
            org=self.influx_org,
            timeout=30_000,
        )

    def set_c2_state(self, algorithm: str, setpoint: float, trial_id: str) -> bool:
        """Dispatch active control law, setpoint, and trial ID to C2 REST API."""
        endpoint = f"{self.c2_url}/api/control"
        payload = {
            "is_running": True,
            "algorithm": algorithm,
            "setpoint": float(setpoint),
            "trial_id": trial_id,
        }
        try:
            resp = requests.post(endpoint, json=payload, timeout=3.0)
            return resp.status_code == 200
        except requests.RequestException as e:
            logger.error("C2 POST /api/control request failed: %s", e)
            return False

    def stop_c2_actuation(self) -> None:
        """Issue halt signal to C2 REST API to disengage plant actuation."""
        endpoint = f"{self.c2_url}/api/stop"
        try:
            requests.post(endpoint, timeout=3.0)
            logger.info("C2 actuation safely de-energized.")
        except requests.RequestException:
            pass

    def export_trial_csv(self, trial_id: str, filename: str) -> None:
        """Extract synchronized trial time-series from InfluxDB and save as standardized CSV."""
        query_api = self.influx_client.query_api()
        flux_query = f"""
        from(bucket: "{self.influx_bucket}")
          |> range(start: -30m)
          |> filter(fn: (r) => r["_measurement"] == "control_telemetry")
          |> filter(fn: (r) => r["trial_id"] == "{trial_id}")
          |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
          |> sort(columns: ["_time"])
        """
        try:
            df = query_api.query_data_frame(flux_query)
            if isinstance(df, list):
                df = pd.concat(df, ignore_index=True) if df else pd.DataFrame()

            target_path = os.path.join(self.data_export_dir, filename)
            if not df.empty:
                if "algorithm" in df.columns:
                    df["algorithm"] = df["algorithm"].replace({"baseline": "discrete"})

                preferred_cols = [
                    "_time",
                    "trial_id",
                    "tube_type",
                    "tube_length_cm",
                    "tube_inner_dia_mm",
                    "algorithm",
                    "setpoint",
                    "process_variable",
                    "control_signal",
                    "error",
                    "rtt_ms",
                    "is_loss",
                ]
                existing_cols = [c for c in preferred_cols if c in df.columns]
                extra_cols = [c for c in df.columns if c not in preferred_cols]
                export_df = df[existing_cols + extra_cols]
                export_df.to_csv(target_path, index=False)
                logger.info("Exported %d records to %s", len(export_df), target_path)
            else:
                logger.warning(
                    "Empty result set for trial '%s'; skipping CSV export.", trial_id
                )
        except Exception as err:
            logger.error("Flux query failure for trial '%s': %s", trial_id, err)

    def run_trial(self, config: TrialConfig) -> None:
        """Execute an isolated step response trial under target network conditions."""
        try:
            self.tc.apply_netem(config.netem_args)
            if not self.set_c2_state(
                config.algorithm, config.setpoint, config.trial_id
            ):
                raise RuntimeError("C2 rejected control configuration.")

            time.sleep(config.duration_sec)

        finally:
            self.stop_c2_actuation()
            self.tc.clear_rules()

        time.sleep(1.5)  # Buffer flush allowance
        self.export_trial_csv(config.trial_id, config.export_filename)

    def _generate_trial_matrix(
        self, v1_dur: float, v2_dur: float, v3_dur: float
    ) -> List[TrialConfig]:
        """Pre-computes the entire experimental execution queue with standardized names."""
        trials: List[TrialConfig] = []
        batch_ts = time.strftime("%Y%m%d_%H%M%S")

        # Vector 1: Dead-time sweeps (discrete vs smith)
        for rtt_ms in range(10, 110, 10):
            for algo in ["discrete", "smith"]:
                t_id = f"sweep_deadtime_{algo}_{rtt_ms}ms_rtt_{batch_ts}"
                fname = f"deadtime_{algo}_{rtt_ms}ms_rtt_{batch_ts}.csv"
                trials.append(
                    TrialConfig(
                        trial_id=t_id,
                        algorithm=algo,
                        setpoint=50.0,
                        netem_args=["delay", f"{rtt_ms // 2}ms"],
                        duration_sec=v1_dur,
                        export_filename=fname,
                    )
                )

        # Vector 2: Stochastic jitter distributions (discrete, smith, resilient)
        jitter_profiles = [
            ("gaussian", ["delay", "40ms", "15ms", "distribution", "normal"]),
            ("pareto", ["delay", "40ms", "15ms", "distribution", "pareto"]),
        ]
        for name, args in jitter_profiles:
            for algo in ["discrete", "smith", "resilient"]:
                t_id = f"sweep_jitter_{name}_{algo}_{batch_ts}"
                fname = f"trial_jitter_{name}_{algo}_{batch_ts}.csv"
                trials.append(
                    TrialConfig(
                        trial_id=t_id,
                        algorithm=algo,
                        setpoint=50.0,
                        netem_args=args,
                        duration_sec=v2_dur,
                        export_filename=fname,
                    )
                )

        # Vector 3: Burst loss profiles (discrete vs resilient)
        for loss_pct in [2, 5, 10, 15, 20]:
            for algo in ["discrete", "resilient"]:
                t_id = f"sweep_burst_loss_{loss_pct}pct_{algo}_{batch_ts}"
                fname = f"burst_loss_{loss_pct}pct_{algo}_{batch_ts}.csv"
                trials.append(
                    TrialConfig(
                        trial_id=t_id,
                        algorithm=algo,
                        setpoint=50.0,
                        netem_args=["loss", f"{loss_pct}%", "25%"],
                        duration_sec=v3_dur,
                        export_filename=fname,
                    )
                )

        return trials

    def execute_matrix(self, v1_dur: float, v2_dur: float, v3_dur: float) -> None:
        """Execute automated benchmark sweeps with dynamic ETA tracking."""
        trials = self._generate_trial_matrix(v1_dur, v2_dur, v3_dur)
        total_trials = len(trials)
        logger.info(
            "Generated empirical sweep matrix: %d total trials scheduled.", total_trials
        )

        start_time = time.time()
        for idx, config in enumerate(trials, start=1):
            if idx == 1:
                eta_seconds = (config.duration_sec + 1.5) * total_trials
            else:
                elapsed = time.time() - start_time
                avg_time_per_trial = elapsed / (idx - 1)
                eta_seconds = avg_time_per_trial * (total_trials - idx + 1)

            eta_str = time.strftime("%H:%M:%S", time.gmtime(eta_seconds))

            logger.info(
                "--- Launching Trial [%d/%d] | ETA: %s | ID: %s (%s) | Duration: %.1fs ---",
                idx,
                total_trials,
                eta_str,
                config.trial_id,
                config.algorithm,
                config.duration_sec,
            )
            self.run_trial(config)


def prompt_and_execute_postprocessing() -> None:
    """Prompt the operator to trigger the data aggregation pipeline."""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    postprocess_script = os.path.normpath(
        os.path.join(current_dir, "..", "analysis", "aggregate_and_postprocess.py")
    )
    display_path = (
        os.path.relpath(postprocess_script)
        if "resilient-wireless-pid" in current_dir
        else postprocess_script
    )
    manual_cmd = f"python3 {display_path}"

    try:
        choice = (
            input(
                "\n[?] Sweep complete. Execute data aggregation and post-processing? (y/N): "
            )
            .strip()
            .lower()
        )
        if choice in ["y", "yes"]:
            logger.info("Launching post-processing pipeline...")
            subprocess.run([sys.executable, postprocess_script], check=True)
        else:
            logger.info(
                "Post-processing skipped. To run manually later, execute:\n\t%s",
                manual_cmd,
            )
    except subprocess.CalledProcessError as e:
        logger.error("Post-processing script failed with exit code %d.", e.returncode)
    except KeyboardInterrupt:
        print()
        logger.info(
            "Post-processing aborted. To run manually later, execute:\n\t%s", manual_cmd
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Automated Network Impairment Parameter Sweep Orchestrator"
    )
    parser.add_argument(
        "--interface",
        type=str,
        default=os.getenv("DCS_IFACE", "wlan0"),
        help="Target egress network interface for tc/netem injection",
    )
    parser.add_argument(
        "--c2-url",
        type=str,
        default="http://127.0.0.1:5050",
        help="Supervisory C2 REST API endpoint",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Global trial duration override in seconds across all vectors",
    )
    parser.add_argument(
        "--v1-duration",
        type=float,
        default=30.0,
        help="Duration for Vector 1 dead-time trials (default: 30.0s)",
    )
    parser.add_argument(
        "--v2-duration",
        type=float,
        default=35.0,
        help="Duration for Vector 2 jitter trials (default: 35.0s)",
    )
    parser.add_argument(
        "--v3-duration",
        type=float,
        default=35.0,
        help="Duration for Vector 3 burst loss trials (default: 35.0s)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    orchestrator = SweepOrchestrator(interface=args.interface, c2_url=args.c2_url)

    if args.duration is not None:
        v1_dur = args.duration
        v2_dur = args.duration
        v3_dur = args.duration
        logger.info(
            "Global duration override active: All vectors configured for %.1f seconds.",
            args.duration,
        )
    else:
        v1_dur = args.v1_duration
        v2_dur = args.v2_duration
        v3_dur = args.v3_duration
        logger.info(
            "Per-vector durations active -> Vector 1: %.1fs | Vector 2: %.1fs | Vector 3: %.1fs",
            v1_dur,
            v2_dur,
            v3_dur,
        )

    logger.info("Starting automated parameter sweeps on interface %s", args.interface)

    sweep_completed_successfully = False
    try:
        orchestrator.execute_matrix(v1_dur=v1_dur, v2_dur=v2_dur, v3_dur=v3_dur)
        sweep_completed_successfully = True
    except KeyboardInterrupt:
        logger.warning("Interrupted by operator; cleaning up qdisc rules...")
        orchestrator.tc.clear_rules()
        orchestrator.stop_c2_actuation()
    except Exception as e:
        logger.error("Fatal error during sweep execution: %s", e)
        orchestrator.tc.clear_rules()
        orchestrator.stop_c2_actuation()
    finally:
        logger.info("Sweep execution sequence terminated.")

        if sweep_completed_successfully:
            prompt_and_execute_postprocessing()
        else:
            current_dir = os.path.dirname(os.path.abspath(__file__))
            postprocess_script = os.path.normpath(
                os.path.join(
                    current_dir, "..", "analysis", "aggregate_and_postprocess.py"
                )
            )
            display_path = (
                os.path.relpath(postprocess_script)
                if "resilient-wireless-pid" in current_dir
                else postprocess_script
            )
            logger.info(
                "Execution was aborted. If you wish to aggregate partial data, manually execute:\n\tpython3 %s",
                display_path,
            )


if __name__ == "__main__":
    main()