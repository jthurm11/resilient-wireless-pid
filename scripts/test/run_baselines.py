#!/usr/bin/env python3
"""
scripts/test/run_baselines.py
Empirical Dynamic Baseline Pipeline with Automated Pre-Flight Runway.
Executes unperturbed step-response sequences across target control algorithms.
"""

import argparse
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from typing import List

import pandas as pd
import requests
from influxdb_client import InfluxDBClient

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("BaselineRunner")


@dataclass
class StepPhase:
    """Discrete setpoint phase within a multi-step dynamic trial."""

    setpoint: float
    duration_sec: float


@dataclass
class BaselineTrialConfig:
    """Execution manifest for an isolated dynamic baseline trial."""

    trial_id: str
    algorithm: str
    tube_type: str
    tube_length_cm: float
    tube_inner_dia_mm: float
    phases: List[StepPhase]
    export_filename: str


class TrafficController:
    """Manages Linux kernel tc/netem queuing disciplines to guarantee zero impairment."""

    def __init__(self, interface: str):
        self.interface = interface

    def clear_rules(self) -> None:
        """Flushes egress qdisc rules to ensure unshaped line-rate operation."""
        cmd = ["tc", "qdisc", "del", "dev", self.interface, "root"]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0 and "No such file or directory" not in res.stderr:
            logger.debug("tc cleanup (%s): %s", self.interface, res.stderr.strip())
        else:
            logger.info(
                "Kernel netem cleared on %s: Baseline line-rate active.", self.interface
            )


class BaselineOrchestrator:
    """Coordinates empirical baseline step-response trials."""

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
        """Dispatches active control law and setpoint target to C2 REST API."""
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
        except requests.RequestException as err:
            logger.error("C2 REST POST /api/control error: %s", err)
            return False

    def stop_c2_actuation(self) -> None:
        """Signals C2 REST API to safely de-energize the fan driver."""
        endpoint = f"{self.c2_url}/api/stop"
        try:
            requests.post(endpoint, timeout=3.0)
            logger.info("Plant blower safely de-energized.")
        except requests.RequestException:
            pass

    def run_preflight_runway(self, initial_sp: float, runway_sec: float = 30.0) -> None:
        """
        Engages the blower on an unshaped link to spin up impellers and hover
        the ball at the initial setpoint prior to trial evaluation.
        """
        logger.info(
            ">>> ENGAGING PRE-FLIGHT RUNWAY (%.1fs) | Target SP: %.1f%% <<<",
            runway_sec,
            initial_sp,
        )
        self.tc.clear_rules()

        # Feedforward spool-up using discrete PID
        logger.info("[Pre-Flight] Spooling impellers to establish positive draft...")
        self.set_c2_state(
            algorithm="discrete", setpoint=initial_sp, trial_id="runway_spoolup"
        )

        remaining_runway = max(1.0, runway_sec - 5.0)
        logger.info(
            "[Pre-Flight] Ball airborne; stabilizing at SP=%.1f%% for %.1fs...",
            initial_sp,
            remaining_runway,
        )
        time.sleep(remaining_runway)
        logger.info(
            ">>> PRE-FLIGHT COMPLETE: Plant at steady-state hover. Transitioning to formal trial. <<<"
        )

    def export_trial_csv(self, config: BaselineTrialConfig) -> None:
        """Queries InfluxDB for trial metrics and exports a populated CSV snapshot."""
        query_api = self.influx_client.query_api()
        flux_query = f"""
        from(bucket: "{self.influx_bucket}")
          |> range(start: -30m)
          |> filter(fn: (r) => r["_measurement"] == "control_telemetry")
          |> filter(fn: (r) => r["trial_id"] == "{config.trial_id}")
          |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
          |> sort(columns: ["_time"])
        """
        try:
            df = query_api.query_data_frame(flux_query)
            if isinstance(df, list):
                df = pd.concat(df, ignore_index=True) if df else pd.DataFrame()

            target_path = os.path.join(self.data_export_dir, config.export_filename)
            if not df.empty:
                # Harmonize algorithm name within telemetry snapshot
                if "algorithm" in df.columns:
                    df["algorithm"] = df["algorithm"].replace({"baseline": "discrete"})
                else:
                    df["algorithm"] = config.algorithm

                # Plant and geometry metadata
                df["tube_type"] = config.tube_type
                df["tube_length_cm"] = config.tube_length_cm
                df["tube_inner_dia_mm"] = config.tube_inner_dia_mm

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
                ordered = [c for c in preferred_cols if c in df.columns]
                extra = [c for c in df.columns if c not in preferred_cols]
                export_df = df[ordered + extra]
                export_df.to_csv(target_path, index=False)
                logger.info(
                    "Exported %d samples for trial '%s' to %s",
                    len(export_df),
                    config.trial_id,
                    target_path,
                )
            else:
                logger.warning(
                    "Zero records for trial '%s'; CSV export skipped.", config.trial_id
                )
        except Exception as err:
            logger.error("InfluxDB query failure for '%s': %s", config.trial_id, err)

    def execute_dynamic_trial(
        self, config: BaselineTrialConfig, runway_duration: float = 30.0
    ) -> None:
        """Executes a dynamic step sequence with pre-flight stabilization."""
        try:
            initial_sp = config.phases[0].setpoint
            self.run_preflight_runway(initial_sp=initial_sp, runway_sec=runway_duration)

            logger.info(
                "=== Launching Dynamic Baseline: %s [Tube: %s | Algorithm: %s] ===",
                config.trial_id,
                config.tube_type,
                config.algorithm,
            )

            for idx, phase in enumerate(config.phases, start=1):
                logger.info(
                    "[Phase %d/%d] Step Target: %.1f%% | Duration: %.1fs",
                    idx,
                    len(config.phases),
                    phase.setpoint,
                    phase.duration_sec,
                )
                if not self.set_c2_state(
                    algorithm=config.algorithm,
                    setpoint=phase.setpoint,
                    trial_id=config.trial_id,
                ):
                    raise RuntimeError(f"C2 rejected step target {phase.setpoint}%")

                time.sleep(phase.duration_sec)

        finally:
            self.stop_c2_actuation()
            self.tc.clear_rules()

        time.sleep(2.0)  # Telemetry write flush window
        self.export_trial_csv(config)


def get_step_profile(tube_type: str, total_duration: float) -> List[StepPhase]:
    """Generates operating step profiles matched to tube aerodynamic limits."""
    if tube_type == "cardboard":
        setpoints = [72.0, 82.0, 90.0, 75.0, 85.0]
    else:
        setpoints = [30.0, 55.0, 75.0, 40.0, 65.0, 80.0]

    phase_len = total_duration / len(setpoints)
    return [StepPhase(setpoint=sp, duration_sec=phase_len) for sp in setpoints]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dynamic Multi-Step Baseline Performance Evaluation"
    )
    parser.add_argument(
        "--algorithm",
        type=str,
        choices=["all", "discrete", "smith", "resilient"],
        default="all",
        help="Target control algorithm (default: all)",
    )
    parser.add_argument(
        "--tube-type",
        type=str,
        choices=["acrylic", "cardboard", "sim_lxc", "sim_docker"],
        default="acrylic",
        help="Plant column type (default: acrylic)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=300.0,
        help="Trial duration in seconds per algorithm (default: 300.0s)",
    )
    parser.add_argument(
        "--runway",
        type=float,
        default=30.0,
        help="Pre-flight stabilization runway in seconds (default: 30.0s)",
    )
    parser.add_argument(
        "--interface",
        type=str,
        default=os.getenv("DCS_IFACE", "wlan0"),
        help="Target network device interface",
    )
    parser.add_argument(
        "--c2-url",
        type=str,
        default="http://127.0.0.1:5050",
        help="Supervisory C2 REST API endpoint",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    orchestrator = BaselineOrchestrator(interface=args.interface, c2_url=args.c2_url)

    target_algos = (
        ["discrete", "smith", "resilient"]
        if args.algorithm == "all"
        else [args.algorithm]
    )

    if args.tube_type == "cardboard":
        tube_length_cm, tube_inner_dia_mm = 78.0, 50.8
    else:
        tube_length_cm, tube_inner_dia_mm = 45.72, 51.0

    phases = get_step_profile(tube_type=args.tube_type, total_duration=args.duration)
    session_ts = time.strftime("%Y%m%d_%H%M%S")

    try:
        for algo in target_algos:
            trial_id = f"dynamic_baseline_{args.tube_type}_{algo}_{session_ts}"
            export_filename = (
                f"dynamic_baseline_{args.tube_type}_{algo}_{session_ts}.csv"
            )

            config = BaselineTrialConfig(
                trial_id=trial_id,
                algorithm=algo,
                tube_type=args.tube_type,
                tube_length_cm=tube_length_cm,
                tube_inner_dia_mm=tube_inner_dia_mm,
                phases=phases,
                export_filename=export_filename,
            )
            orchestrator.execute_dynamic_trial(config, runway_duration=args.runway)

    except KeyboardInterrupt:
        logger.warning("Operator interrupt detected. Shutting down...")
        orchestrator.stop_c2_actuation()
        orchestrator.tc.clear_rules()
    except Exception as err:
        logger.error("Execution aborted: %s", err)
        orchestrator.stop_c2_actuation()
        orchestrator.tc.clear_rules()


if __name__ == "__main__":
    main()
