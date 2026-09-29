#!/usr/bin/env python3
"""
scripts/test/run_sweeps.py
Automated parameter sweep orchestrator and telemetry snapshot pipeline.
Coordinates tc/netem kernel qdiscs, C2 REST states, and InfluxDB CSV exports.
"""

import argparse
import logging
import os
import subprocess
import time

import pandas as pd
import requests
from influxdb_client import InfluxDBClient

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("SweepOrchestrator")


class TrafficController:
    """Manages Linux kernel tc/netem queuing disciplines via iproute2."""

    def __init__(self, interface: str):
        """
        Initialize traffic controller interface binding.

        Args:
            interface (str): Target network interface for egress traffic shaping.
        """
        self.interface = interface

    def clear_rules(self) -> None:
        """Purge root qdisc to restore clean, unshaped line-rate conditions."""
        cmd = ["tc", "qdisc", "del", "dev", self.interface, "root"]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0 and "No such file or directory" not in res.stderr:
            logger.debug(
                "tc cleanup notice (%s): %s", self.interface, res.stderr.strip()
            )

    def apply_netem(self, netem_args: list[str]) -> None:
        """
        Attach configured netem qdisc rules to root egress.

        Args:
            netem_args (list[str]): Positional CLI arguments passed to netem.
        """
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
        data_export_dir: str = "/data_exports",
    ):
        """
        Initialize orchestrator clients, endpoints, and storage targets.

        Args:
            interface (str): Network device interface for traffic shaping.
            c2_url (str): Supervisory C2 HTTP REST endpoint base URL.
            influx_url (str): InfluxDB v2 server address.
            influx_token (str): InfluxDB organization authentication token.
            influx_org (str): InfluxDB destination organization namespace.
            influx_bucket (str): InfluxDB telemetry bucket name.
            data_export_dir (str): Filesystem directory for persisting CSV snapshots.
        """
        self.tc = TrafficController(interface)
        self.c2_url = c2_url.rstrip("/")
        self.influx_url = influx_url
        self.influx_token = influx_token
        self.influx_org = influx_org
        self.influx_bucket = influx_bucket
        self.data_export_dir = data_export_dir

        # Ensure persistence destination directory exists
        os.makedirs(self.data_export_dir, exist_ok=True)
        self.influx_client = InfluxDBClient(
            url=self.influx_url,
            token=self.influx_token,
            org=self.influx_org,
            timeout=30_000,
        )

    def set_c2_state(self, algorithm: str, setpoint: float, trial_id: str) -> bool:
        """
        Dispatch active control law and setpoint target to C2 REST API.

        Args:
            algorithm (str): Active control algorithm identifier.
            setpoint (float): Reference trajectory setpoint.
            trial_id (str): Unique trial execution tag.

        Returns:
            bool: True if REST transaction succeeded with HTTP 200.
        """
        endpoint = f"{self.c2_url}/api/control"
        payload = {
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
        """
        Extract synchronized trial time-series from InfluxDB and save as CSV.

        Args:
            trial_id (str): Experiment trial identifier tag to isolate.
            filename (str): Name of destination CSV file in data_export_dir.
        """
        query_api = self.influx_client.query_api()
        flux_query = f"""
        from(bucket: "{self.influx_bucket}")
          |> range(start: -30m)
          |> filter(fn: (r) => r["_measurement"] == "control_telemetry")
          |> filter(fn: (r) => r["trial_id"] == "{trial_id}")
          |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
          |> keep(columns: ["_time", "trial_id", "algorithm", "setpoint",
                           "process_variable", "control_signal", "error",
                           "rtt_ms", "is_loss"])
          |> sort(columns: ["_time"])
        """
        df = query_api.query_data_frame(flux_query)
        if isinstance(df, list):
            df = pd.concat(df, ignore_index=True) if df else pd.DataFrame()

        target_path = os.path.join(self.data_export_dir, filename)
        if not df.empty:
            df.to_csv(target_path, index=False)
            logger.info("Exported %d records to %s", len(df), target_path)
        else:
            logger.warning(
                "Empty result set for trial '%s'; skipping CSV export.", trial_id
            )

    def run_trial(
        self,
        trial_id: str,
        algorithm: str,
        setpoint: float,
        netem_args: list[str],
        duration_sec: float,
        export_filename: str,
    ) -> None:
        """
        Execute an isolated step response trial under target network conditions.

        Args:
            trial_id (str): Unique trial execution tag.
            algorithm (str): Active control law.
            setpoint (float): Target reference setpoint.
            netem_args (list[str]): Traffic shaping rules for the interface.
            duration_sec (float): Active runtime observation window.
            export_filename (str): Output CSV filename for dataset extraction.
        """
        logger.info("--- Launching Trial: %s (%s) ---", trial_id, algorithm)
        try:
            # Apply kernel traffic shaping rules
            self.tc.apply_netem(netem_args)

            # Update C2 runtime state
            if not self.set_c2_state(algorithm, setpoint, trial_id):
                raise RuntimeError("C2 rejected control configuration.")

            # Monitor loop duration window
            time.sleep(duration_sec)

        finally:
            # Ensure physical plant is halted and kernel network rules are reset
            self.stop_c2_actuation()
            self.tc.clear_rules()

        # Allow InfluxDB asynchronous worker buffers to flush before querying
        time.sleep(1.5)
        self.export_trial_csv(trial_id, export_filename)

    def execute_matrix(self) -> None:
        """Execute automated benchmark sweeps across all experimental vectors."""
        # Vector 1: Pure dead-time phase margin erosion sweeps (10ms to 100ms RTT)
        for rtt_ms in range(10, 110, 10):
            one_way_delay = f"{rtt_ms // 2}ms"
            for algo in ["baseline", "smith"]:
                t_id = f"sweep_deadtime_{algo}_{rtt_ms}ms_rtt"
                fname = (
                    f"control_law_comparison_{rtt_ms}ms_rtt.csv"
                    if algo == "smith"
                    else f"baseline_{rtt_ms}ms_rtt.csv"
                )
                self.run_trial(
                    trial_id=t_id,
                    algorithm=algo,
                    setpoint=50.0,
                    netem_args=["delay", one_way_delay],
                    duration_sec=20.0,
                    export_filename=fname,
                )

        # Vector 2: Stochastic delay jitter distributions (Gaussian vs. Pareto)
        jitter_profiles = [
            ("gaussian", ["delay", "40ms", "15ms", "distribution", "normal"]),
            ("pareto", ["delay", "40ms", "15ms", "distribution", "pareto"]),
        ]
        for name, args in jitter_profiles:
            for algo in ["baseline", "smith", "resilient"]:
                t_id = f"sweep_jitter_{name}_{algo}"
                fname = f"trial_jitter_{name}_{algo}.csv"
                self.run_trial(
                    trial_id=t_id,
                    algorithm=algo,
                    setpoint=50.0,
                    netem_args=args,
                    duration_sec=25.0,
                    export_filename=fname,
                )

        # Vector 3: Correlated burst dropouts (p_loss in [2%, 20%], correlation=25%)
        for loss_pct in [2, 5, 10, 15, 20]:
            for algo in ["baseline", "resilient"]:
                t_id = f"sweep_burst_loss_{loss_pct}pct_{algo}"
                fname = f"burst_loss_{loss_pct}pct_{algo}.csv"
                self.run_trial(
                    trial_id=t_id,
                    algorithm=algo,
                    setpoint=50.0,
                    netem_args=["loss", f"{loss_pct}%", "25%"],
                    duration_sec=25.0,
                    export_filename=fname,
                )


def parse_args() -> argparse.Namespace:
    """
    Parse command-line arguments for sweep automation.

    Returns:
        argparse.Namespace: Parsed CLI configuration flags.
    """
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
    return parser.parse_args()


def main() -> None:
    """Initialize orchestrator dependencies and run parameter sweep matrix."""
    args = parse_args()
    orchestrator = SweepOrchestrator(interface=args.interface, c2_url=args.c2_url)

    logger.info("Starting automated parameter sweeps on interface %s", args.interface)
    try:
        orchestrator.execute_matrix()
    except KeyboardInterrupt:
        logger.warning("Interrupted by operator; cleaning up qdisc rules...")
        orchestrator.tc.clear_rules()
        orchestrator.stop_c2_actuation()
    logger.info("Sweep execution completed.")


if __name__ == "__main__":
    main()