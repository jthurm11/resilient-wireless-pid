#!/usr/bin/env python3
"""
src/resilient_pid/main.py
Main runtime orchestrator and CLI entry point for resilient-wireless-pid.
"""
import os
import sys
import time
import socket
import json
import logging
import argparse
import threading
import subprocess
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


def is_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """Check if the C2 port is already occupied by an active process."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        return s.connect_ex((host, port)) == 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Resilient Wireless DCS Runtime Controller")
    parser.add_argument("--mode", choices=["baseline", "smith", "resilient"], default="baseline")
    parser.add_argument("--steps", type=int, default=500)
    parser.add_argument("--dt", type=float, default=0.05)
    parser.add_argument("--setpoint", type=float, default=50.0)
    parser.add_argument("--target-host", type=str, default=os.getenv("PLANT_IP", "10.10.10.2"))
    parser.add_argument("--target-port", type=int, default=int(os.getenv("PLANT_PORT", "5005")))
    parser.add_argument("--trial-id", type=str, default="")
    parser.add_argument("--c2-port", type=int, default=int(os.getenv("C2_PORT", "5000")))
    parser.add_argument("--no-c2", action="store_true", help="Disable C2 sync and auto-spawning completely")
    parser.add_argument("--enable-ui", action="store_true", help="Instruct spawned C2 instance to serve test web UI")
    parser.add_argument("--mock-loop", action="store_true", help="Simulate first-order plant locally without UDP")
    return parser.parse_args()


class ControllerRuntime:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.c2_url = f"http://127.0.0.1:{args.c2_port}"
        self.plant_addr = (args.target_host, args.target_port)

        self.dt = args.dt
        self.setpoint = args.setpoint
        self.mode = args.mode
        self.trial_id = args.trial_id or f"{args.mode}_{int(time.time())}"
        self.is_running = True

        self.kp, self.ki, self.kd = 1.2, 0.4, 0.05
        self.seq_num = 0
        self.last_known_pv = 0.0

        self.controllers = {
            "baseline": DiscretePID(kp=self.kp, ki=self.ki, kd=self.kd, dt=self.dt, output_limits=(0.0, 100.0)),
            "smith": SmithPredictor(kp=self.kp, ki=self.ki, kd=self.kd, dt=self.dt, output_limits=(0.0, 100.0)),
            "resilient": ResilientPID(kp=self.kp, ki=self.ki, kd=self.kd, dt=self.dt, output_limits=(0.0, 100.0)),
        }

        self.lock = threading.Lock()
        self._stop_event = threading.Event()

        self.telemetry = InfluxWriter(
            url=os.getenv("INFLUXDB_URL", "http://localhost:8086"),
            token=os.getenv("INFLUXDB_TOKEN", "testbed_secret_token_123"),
            org=os.getenv("INFLUXDB_ORG", "resilient_pid"),
            bucket=os.getenv("INFLUXDB_BUCKET", "wireless_pid_metrics"),
            dry_run=os.getenv("INFLUXDB_DRY_RUN", "False").lower() == "true"
        )

        self._socket: Optional[socket.socket] = None
        if not self.args.mock_loop:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._socket.settimeout(self.dt * 0.8)

    def start(self) -> None:
        logger.info("Starting Controller | Target Plant: %s | Mode: %s", self.plant_addr, self.mode)
        if hasattr(self.telemetry, "start"):
            self.telemetry.start()

        if not self.args.no_c2:
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
        logger.info("Controller halted.")

    def _c2_sync_loop(self) -> None:
        endpoint = f"{self.c2_url}/api/status"
        while not self._stop_event.is_set():
            try:
                res = requests.get(endpoint, timeout=0.8)
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

        for _ in range(self.args.steps if self.args.no_c2 else sys.maxsize):
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

            if active_mode == "resilient":
                u_t = controller.update(setpoint=sp, pv_actual=self.last_known_pv, is_loss=False)
            else:
                u_t = controller.update(setpoint=sp, pv=self.last_known_pv)

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

            if is_loss and active_mode == "resilient":
                u_t = controller.update(setpoint=sp, pv_actual=self.last_known_pv, is_loss=True)
                self.last_known_pv = controller.y_est

            error = sp - self.last_known_pv
            self.telemetry.log_control_metrics(
                trial_id=trial,
                algorithm=active_mode,
                setpoint=sp,
                process_variable=self.last_known_pv,
                control_signal=u_t,
                error=error,
                rtt_ms=rtt_ms
            )

            self.seq_num += 1
            next_tick += dt
            sleep_rem = next_tick - time.perf_counter()
            if sleep_rem > 0:
                time.sleep(sleep_rem)
            else:
                next_tick = time.perf_counter()


def sync_c2_ui_preference(port: int, enable_ui: bool) -> None:
    """Explicitly toggle the C2 server UI state via REST IPC."""
    try:
        requests.post(
            f"http://127.0.0.1:{port}/api/control",
            json={"ui_enabled": enable_ui},
            timeout=1.0
        )
    except requests.RequestException:
        pass


def main() -> None:
    args = parse_args()
    c2_proc = None
    c2_log_file = None

    # Handle C2 lifecycle and UI synchronization
    if not args.no_c2:
        if is_port_in_use(args.c2_port):
            logger.info("Existing C2 server detected on port %d. Synchronizing UI preference...", args.c2_port)
            sync_c2_ui_preference(args.c2_port, args.enable_ui)
        else:
            logger.info("Port %d vacant. Spawning background C2 process (logs -> /tmp/c2_server.log)...", args.c2_port)
            c2_log_file = open("/tmp/c2_server.log", "a")
            cmd = [sys.executable, "-m", "resilient_pid.c2.c2_server", "--port", str(args.c2_port)]
            if args.enable_ui:
                cmd.append("--enable-ui")

            # Route subprocess standard streams away from current terminal
            c2_proc = subprocess.Popen(
                cmd,
                stdout=c2_log_file,
                stderr=c2_log_file
            )
            time.sleep(1.0)  # Yield to permit socket bind

    runtime = ControllerRuntime(args)
    try:
        runtime.start()
    except KeyboardInterrupt:
        logger.info("Termination signal caught.")
    finally:
        runtime.stop()
        if c2_proc:
            logger.info("Terminating spawned C2 background process...")
            c2_proc.terminate()
            c2_proc.wait()
        if c2_log_file:
            c2_log_file.close()


if __name__ == "__main__":
    main()