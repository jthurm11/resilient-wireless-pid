#!/usr/bin/env python3
"""
src/resilient_pid/main.py
Main runtime orchestrator and CLI entry point for resilient-wireless-pid.
Supports standalone batch sweeps as well as live C2 state synchronization.
"""
import os
import sys
import time
import socket
import json
import logging
import argparse
import threading
from typing import Optional

import requests

from resilient_pid.controller.pid import DiscretePID
from resilient_pid.controller.smith_predictor import SmithPredictor
from resilient_pid.controller.resilient_pid import ResilientPID

try:
    from resilient_pid.telemetry.influx_writer import InfluxWriter
except ImportError:
    from influx_writer import InfluxWriter

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("DCSControllerMain")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Resilient Wireless DCS Runtime Controller")
    parser.add_argument("--mode", choices=["baseline", "smith", "resilient"], default="baseline",
                        help="Control law architecture under evaluation")
    parser.add_argument("--steps", type=int, default=500, help="Total control steps to execute")
    parser.add_argument("--dt", type=float, default=0.05, help="Sampling period in seconds (default: 50ms)")
    parser.add_argument("--setpoint", type=float, default=100.0, help="Target process variable setpoint")
    parser.add_argument("--target-host", type=str, default=os.getenv("PLANT_IP", "10.10.10.2"),
                        help="Plant UDP network host IP")
    parser.add_argument("--target-port", type=int, default=int(os.getenv("PLANT_PORT", "5005")),
                        help="Plant UDP target port")
    parser.add_argument("--trial-id", type=str, default="", help="Unique experiment trial identifier")
    parser.add_argument("--c2-sync", action="store_true", help="Enable continuous C2 REST polling thread")
    parser.add_argument("--mock-loop", action="store_true", help="Simulate first-order plant locally without UDP")
    return parser.parse_args()


class ControllerRuntime:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.c2_url = os.getenv("C2_URL", "http://localhost:5000")
        self.plant_addr = (args.target_host, args.target_port)

        self.dt = args.dt
        self.setpoint = args.setpoint
        self.mode = args.mode
        self.trial_id = args.trial_id or f"{args.mode}_{int(time.time())}"
        self.is_running = True

        self.kp, self.ki, self.kd = 1.2, 0.4, 0.05
        self.seq_num = 0
        self.last_known_pv = 0.0

        # Modular Control Law instances
        self.controllers = {
            "baseline": DiscretePID(kp=self.kp, ki=self.ki, kd=self.kd, dt=self.dt, output_limits=(-100.0, 100.0)),
            "smith": SmithPredictor(kp=self.kp, ki=self.ki, kd=self.kd, dt=self.dt, output_limits=(-100.0, 100.0)),
            "resilient": ResilientPID(kp=self.kp, ki=self.ki, kd=self.kd, dt=self.dt, output_limits=(-100.0, 100.0)),
        }

        self.lock = threading.Lock()
        self._stop_event = threading.Event()

        # Telemetry Initializer
        influx_url = os.getenv("INFLUXDB_URL", "http://localhost:8086")
        influx_token = os.getenv("INFLUXDB_TOKEN", "testbed_secret_token_123")
        influx_org = os.getenv("INFLUXDB_ORG", "resilient_pid")
        influx_bucket = os.getenv("INFLUXDB_BUCKET", "wireless_pid_metrics")
        dry_run = os.getenv("INFLUXDB_DRY_RUN", "False").lower() == "true"

        self.telemetry = InfluxWriter(
            url=influx_url,
            token=influx_token,
            org=influx_org,
            bucket=influx_bucket,
            dry_run=dry_run
        )

        self._socket: Optional[socket.socket] = None
        if not self.args.mock_loop:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._socket.settimeout(self.dt * 0.8)

    def start(self) -> None:
        logger.info("Initializing runtime execution | Mode: %s | Target: %s", self.mode, self.plant_addr)
        if hasattr(self.telemetry, "start"):
            self.telemetry.start()

        if self.args.c2_sync:
            sync_thread = threading.Thread(target=self._c2_sync_loop, name="C2SyncWorker", daemon=True)
            sync_thread.start()

        self._run_sampling_loop()

    def stop(self) -> None:
        self._stop_event.set()
        if self._socket:
            self._socket.close()
        if hasattr(self.telemetry, "stop"):
            self.telemetry.stop()
        elif hasattr(self.telemetry, "close"):
            self.telemetry.close()
        logger.info("Controller runtime successfully stopped.")

    def _c2_sync_loop(self) -> None:
        endpoint = f"{self.c2_url}/api/status"
        while not self._stop_event.is_set():
            try:
                res = requests.get(endpoint, timeout=1.0)
                if res.status_code == 200:
                    data = res.json()
                    with self.lock:
                        self.is_running = data.get("is_running", self.is_running)
                        self.trial_id = data.get("trial_id", self.trial_id)
                        mode_map = {"standard_pid": "baseline", "smith_predictor": "smith", "resilient_pid": "resilient"}
                        raw_alg = data.get("algorithm", self.mode)
                        self.mode = mode_map.get(raw_alg, raw_alg)
                        self.setpoint = float(data.get("setpoint", self.setpoint))
            except requests.RequestException:
                pass
            time.sleep(1.0)

    def _run_sampling_loop(self) -> None:
        next_tick = time.perf_counter()
        mock_pv = 0.0

        for _ in range(self.args.steps if not self.args.c2_sync else sys.maxsize):
            if self._stop_event.is_set():
                break

            with self.lock:
                if not self.is_running:
                    for ctrl in self.controllers.values():
                        ctrl.reset()
                    self.seq_num = 0
                    self.last_known_pv = 0.0
                    time.sleep(0.01)
                    next_tick = time.perf_counter()
                    continue
                active_mode = self.mode
                sp = self.setpoint
                dt = self.dt
                trial = self.trial_id

            controller = self.controllers.get(active_mode, self.controllers["baseline"])
            is_loss = False
            rtt_ms = None

            # 1. Forward Control Effort Calculation
            if active_mode == "resilient":
                u_t = controller.update(setpoint=sp, pv_actual=self.last_known_pv, is_loss=False)
            else:
                u_t = controller.update(setpoint=sp, pv=self.last_known_pv)

            # 2. Plant Interaction (Local Synthesis vs. UDP Socket)
            if self.args.mock_loop:
                mock_pv += (u_t * 0.1) - (mock_pv * 0.02)
                self.last_known_pv = mock_pv
                rtt_ms = 0.5
            else:
                t_tx = time.perf_counter()
                payload = json.dumps({"seq": self.seq_num, "u": u_t, "t_send": t_tx}).encode("utf-8")
                try:
                    self._socket.sendto(payload, self.plant_addr)
                    raw, _ = self._socket.recvfrom(1024)
                    rtt_ms = (time.perf_counter() - t_tx) * 1000.0
                    resp = json.loads(raw.decode("utf-8"))

                    if resp.get("seq") == self.seq_num:
                        self.last_known_pv = float(resp["pv"])
                    else:
                        is_loss = True
                except (socket.timeout, ConnectionRefusedError, json.JSONDecodeError):
                    is_loss = True
                    rtt_ms = None

            # 3. Observer Compensation on Packet Drop
            if is_loss and active_mode == "resilient":
                u_t = controller.update(setpoint=sp, pv_actual=self.last_known_pv, is_loss=True)
                self.last_known_pv = controller.y_est

            # 4. Telemetry Stream Logging
            error = sp - self.last_known_pv
            if hasattr(self.telemetry, "log_control_metrics"):
                self.telemetry.log_control_metrics(
                    trial_id=trial,
                    algorithm=active_mode,
                    setpoint=sp,
                    process_variable=self.last_known_pv,
                    control_signal=u_t,
                    error=error,
                    rtt_ms=rtt_ms
                )
            elif hasattr(self.telemetry, "write_point"):
                self.telemetry.write_point(
                    measurement="control_telemetry",
                    tags={"trial_id": trial, "algorithm": active_mode},
                    fields={
                        "setpoint": float(sp),
                        "process_variable": float(self.last_known_pv),
                        "control_signal": float(u_t),
                        "error": float(error),
                        "rtt": float((rtt_ms / 1000.0) if rtt_ms is not None else (dt * 0.8)),
                        "packet_loss": 1 if is_loss else 0
                    }
                )

            # 5. Drift-Compensated Monotonic Clock Synchronization
            self.seq_num += 1
            next_tick += dt
            sleep_rem = next_tick - time.perf_counter()
            if sleep_rem > 0:
                time.sleep(sleep_rem)
            else:
                next_tick = time.perf_counter()


def main() -> None:
    args = parse_args()
    runtime = ControllerRuntime(args)
    try:
        runtime.start()
    except KeyboardInterrupt:
        logger.info("Operator interruption captured.")
    finally:
        runtime.stop()


if __name__ == "__main__":
    main()