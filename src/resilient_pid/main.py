#!/usr/bin/env python3
"""
src/resilient_pid/main.py
Main runtime orchestrator and real-time execution loop for resilient-wireless-pid.
Coordinates supervisory C2 state, real-time UDP telemetry, and InfluxDB persistence.
"""

import argparse
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Protocol

import requests

from resilient_pid.controller.arx_model import ARXModelParams
from resilient_pid.controller.pid import DiscretePID, PIDConfig
from resilient_pid.controller.resilient_pid import ResilientPID
from resilient_pid.controller.smith_predictor import SmithPredictor
from resilient_pid.telemetry.influx_writer import InfluxWriter

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("DCSControllerMain")


class ControllerProtocol(Protocol):
    """Structural protocol for DCS controller implementations."""

    def reset(self) -> None: ...

    def update(self, *args: Any, **kwargs: Any) -> float: ...


ControllerType = DiscretePID | SmithPredictor | ResilientPID


def is_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """Verify if the C2 HTTP socket is already bound."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        return s.connect_ex((host, port)) == 0


def parse_args() -> argparse.Namespace:
    """Parse runtime command-line configuration arguments."""
    parser = argparse.ArgumentParser(
        description="Resilient Wireless DCS Runtime Controller"
    )
    parser.add_argument(
        "--mode",
        choices=["discrete", "smith", "resilient"],
        default="discrete",
        help="Control law architecture under evaluation",
    )
    parser.add_argument(
        "--steps", type=int, default=500, help="Total steps to execute (if no-c2)"
    )
    parser.add_argument(
        "--dt",
        type=float,
        default=0.05,
        help="Sampling period in seconds (default: 50ms)",
    )
    parser.add_argument(
        "--setpoint", type=float, default=None, help="Target process variable setpoint"
    )
    parser.add_argument(
        "--target-host",
        type=str,
        default=os.getenv("PLANT_IP", "10.10.10.2"),
        help="Target plant UDP network host IP",
    )
    parser.add_argument(
        "--target-port",
        type=int,
        default=int(os.getenv("PLANT_PORT", "5005")),
        help="Target plant UDP port",
    )
    parser.add_argument(
        "--trial-id", type=str, default="", help="Unique experiment trial identifier"
    )
    parser.add_argument(
        "--c2-port",
        type=int,
        default=int(os.getenv("C2_PORT", "5050")),
        help="Supervisory C2 HTTP port",
    )
    parser.add_argument(
        "--no-c2",
        action="store_true",
        help="Disable C2 sync and auto-spawning entirely",
    )
    parser.add_argument(
        "--enable-ui", action="store_true", help="Mount minimal operator web UI at /"
    )
    parser.add_argument(
        "--mock-loop",
        action="store_true",
        help="Simulate first-order plant locally without UDP",
    )
    return parser.parse_args()


def wait_for_c2_ready(
    port: int, host: str = "127.0.0.1", timeout: float = 10.0
) -> bool:
    """Polls C2 TCP socket until reachable or timeout expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return True
        except (OSError, ConnectionRefusedError):
            time.sleep(0.2)
    return False


def push_c2_configuration(port: int, payload: dict[str, Any]) -> bool:
    """Transmits startup configuration payload to C2 REST endpoint."""
    if not wait_for_c2_ready(port, timeout=8.0):
        logger.warning("C2 server failed to bind port %d within timeout window.", port)
        return False

    url = f"http://127.0.0.1:{port}/api/control"
    for _ in range(5):
        try:
            res = requests.post(url, json=payload, timeout=1.0)
            if res.status_code == 200:
                logger.info("Synchronized active parameters to C2: %s", payload)
                return True
        except requests.RequestException:
            time.sleep(0.5)

    logger.warning("Could not reach C2 on port %d to push startup parameters.", port)
    return False


class ControllerRuntime:
    """Executes the discrete real-time DCS control and telemetry loop."""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.c2_url = f"http://127.0.0.1:{args.c2_port}"
        self.plant_addr = (args.target_host, args.target_port)

        self.dt = args.dt
        self.setpoint = args.setpoint if args.setpoint is not None else 50.0
        self.mode = args.mode
        self.trial_id = args.trial_id or (
            f"{args.mode}_{int(time.time())}" if args.no_c2 else ""
        )
        self.is_running = True

        # Centralized Single Source of Truth for Loop Gains & Model Parameters
        self.loop_config = PIDConfig(dt=self.dt)
        self.arx_params = ARXModelParams()

        # Resolve expected transport dead time
        is_virtual = self.args.mock_loop or os.getenv("DCS_ENV") in (
            "docker",
            "proxmox_lxc",
        )
        nominal_delay_ms = 0.0 if is_virtual else 18.0

        self.controllers: dict[str, ControllerType] = {
            "discrete": DiscretePID(config=self.loop_config),
            "smith": SmithPredictor(
                config=self.loop_config,
                model_params=self.arx_params,
                plant_delay_ms=nominal_delay_ms,
            ),
            "resilient": ResilientPID(
                config=self.loop_config,
                model_params=self.arx_params,
            ),
        }

        self.seq_num = 0
        self.expected_rx_seq: int | None = None
        self.last_known_pv = 0.0
        self.last_u = self.loop_config.output_limits[0]

        self.lock = threading.Lock()
        self._stop_event = threading.Event()

        self.telemetry = InfluxWriter(
            url=os.getenv("INFLUXDB_URL", "http://localhost:8086"),
            token=os.getenv("INFLUXDB_TOKEN", "testbed_secret_token_123"),
            org=os.getenv("INFLUXDB_ORG", "resilient_pid"),
            bucket=os.getenv("INFLUXDB_BUCKET", "wireless_pid_metrics"),
            dry_run=os.getenv("INFLUXDB_DRY_RUN", "False").lower() == "true",
        )

        self._socket: socket.socket | None = None
        if not self.args.mock_loop:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # Allocate a 64KB receive buffer to absorb burst jitter without packet drop
            self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
            self._drain_socket_buffer()

    def _drain_socket_buffer(self) -> None:
        """Flushes residual kernel socket queues non-blockingly."""
        if self._socket is None:
            return
        original_timeout = self._socket.gettimeout()
        self._socket.setblocking(False)
        drained_count = 0
        try:
            while True:
                _, _ = self._socket.recvfrom(2048)
                drained_count += 1
        except (OSError, BlockingIOError):
            pass
        finally:
            if original_timeout is not None:
                self._socket.settimeout(original_timeout)
            else:
                self._socket.setblocking(True)
        if drained_count > 0:
            logger.info(
                "Purged %d stale frame(s) from kernel UDP queue.", drained_count
            )

    def _sync_initial_c2_state(self) -> None:
        """Query C2 synchronously once prior to loop boot to avoid tag desync."""
        if self.args.no_c2:
            return
        endpoint = f"{self.c2_url}/api/status"
        try:
            res = requests.get(endpoint, timeout=1.0)
            if res.status_code == 200:
                data = res.json()
                with self.lock:
                    if not self.trial_id:
                        self.trial_id = data.get(
                            "trial_id", f"{self.mode}_{int(time.time())}"
                        )
                    if self.args.setpoint is None:
                        self.setpoint = float(data.get("setpoint", self.setpoint))
                    self.is_running = data.get("is_running", self.is_running)
                    logger.info(
                        "Seeded initial runtime state from C2: Trial=%s, SP=%.1f",
                        self.trial_id,
                        self.setpoint,
                    )
        except requests.RequestException as e:
            logger.warning(
                "Failed to reach C2 during startup handshake (%s). Using defaults.", e
            )
            if not self.trial_id:
                self.trial_id = f"{self.mode}_{int(time.time())}"

    def start(self) -> None:
        """Spawns background sync and initiates real-time sampling."""
        self._sync_initial_c2_state()
        logger.info(
            "Starting Controller | Target Plant: %s | Mode: %s | SP: %.2f | Trial: %s",
            self.plant_addr,
            self.mode,
            self.setpoint,
            self.trial_id,
        )

        if hasattr(self.telemetry, "start"):
            self.telemetry.start()

        if not self.args.no_c2:
            sync_thread = threading.Thread(
                target=self._c2_sync_loop, name="C2SyncWorker", daemon=True
            )
            sync_thread.start()

        self._run_sampling_loop()

    def stop(self) -> None:
        """Safely shuts down sockets, workers, and telemetry writers."""
        self._stop_event.set()
        if self._socket:
            self._socket.close()
        if hasattr(self.telemetry, "stop"):
            self.telemetry.stop()
        elif hasattr(self.telemetry, "close"):
            self.telemetry.close()
        logger.info("Controller halted.")

    def _c2_sync_loop(self) -> None:
        """Background thread synchronizing parameters with supervisory C2."""
        endpoint = f"{self.c2_url}/api/status"
        while not self._stop_event.is_set():
            try:
                res = requests.get(endpoint, timeout=0.8)
                if res.status_code == 200:
                    data = res.json()
                    with self.lock:
                        self.is_running = data.get("is_running", self.is_running)
                        self.trial_id = data.get("trial_id", self.trial_id)
                        mode_map = {
                            "standard_pid": "discrete",
                            "standard": "discrete",
                            "baseline": "discrete",
                            "discrete_pid": "discrete",
                            "discrete": "discrete",
                            "smith_predictor": "smith",
                            "resilient_pid": "resilient",
                        }
                        raw_alg = data.get("algorithm", self.mode)
                        self.mode = mode_map.get(raw_alg, raw_alg)
                        self.setpoint = float(data.get("setpoint", self.setpoint))
            except requests.RequestException:
                pass
            time.sleep(1.0)

    def _run_sampling_loop(self) -> None:
        """Executes real-time discrete control step on drift-free schedule."""
        next_tick = time.perf_counter()
        last_tick = time.perf_counter()
        mock_pv = 0.0
        was_running = False  # Track running state transitions

        for _ in range(self.args.steps if self.args.no_c2 else sys.maxsize):
            if self._stop_event.is_set():
                break

            now = time.perf_counter()
            dt_exec_ms = (now - last_tick) * 1000.0
            last_tick = now

            with self.lock:
                active_is_running = self.is_running and (self.setpoint > 0.0)

                if not active_is_running:
                    # FALLING EDGE DETECTOR: Just transitioned from running to stopped
                    if was_running:
                        logger.info(
                            ">>> ACTIVE CONTROL STOPPED: De-energizing plant blower (u=0.0%) <<<"
                        )
                        for ctrl in self.controllers.values():
                            ctrl.reset()
                        self.seq_num = 0
                        self.expected_rx_seq = None
                        self.last_u = 0.0

                        # Dispatch zero-duty datagrams to physical plant over the active socket
                        if self._socket is not None and not self.args.mock_loop:
                            stop_pkt = json.dumps(
                                {"seq": 0, "u": 0.0, "t_send": time.perf_counter()}
                            ).encode("utf-8")
                            for _ in range(3):
                                try:
                                    self._socket.sendto(stop_pkt, self.plant_addr)
                                except OSError as err:
                                    logger.error(
                                        "Failed to transmit stop frame: %s", err
                                    )
                                time.sleep(0.005)

                        self._drain_socket_buffer()
                        was_running = False

                    time.sleep(0.05)
                    next_tick = time.perf_counter()
                    continue

                # If running and setpoint > 0, mark active
                was_running = True
                active_mode = self.mode
                sp = self.setpoint
                dt = self.dt
                trial = self.trial_id

            controller = self.controllers.get(active_mode, self.controllers["discrete"])
            is_loss = False
            rtt_ms = None
            current_pv = self.last_known_pv
            raw_dist = None
            height_val = None
            tach_val = -1.0
            tach_vld = False

            # Transmit Actuation & Sample Process Feedback (Network First)
            if self.args.mock_loop:
                mock_pv += (self.last_u * 0.1) - (mock_pv * 0.02)
                current_pv = mock_pv
                self.last_known_pv = current_pv
                rtt_ms = 0.5
                raw_dist = 45.085 - (current_pv * 0.45085)
                height_val = current_pv * 0.45085
                tach_val = (self.last_u / 100.0) * 3200.0
                tach_vld = True
            else:
                t_tx = time.perf_counter()
                payload = json.dumps(
                    {"seq": self.seq_num, "u": self.last_u, "t_send": t_tx}
                ).encode("utf-8")

                try:
                    if self._socket is not None:
                        self._socket.sendto(payload, self.plant_addr)

                        # Dynamic deadline-bounded poll
                        rx_frame = None
                        loop_deadline = t_tx + (self.dt * 0.96)

                        while True:
                            remaining = loop_deadline - time.perf_counter()
                            if remaining <= 0:
                                break
                            self._socket.settimeout(remaining)
                            try:
                                raw, _ = self._socket.recvfrom(1024)
                                t_rx = time.perf_counter()
                                resp = json.loads(raw.decode("utf-8"))
                                rx_seq = resp.get("seq")

                                if rx_seq is None:
                                    continue

                                # Sliding-Window Resynchronization Tracker
                                if self.expected_rx_seq is None:
                                    self.expected_rx_seq = rx_seq
                                    rx_frame = resp
                                    rtt_ms = (t_rx - t_tx) * 1000.0
                                    break

                                delta = rx_seq - self.expected_rx_seq
                                if delta == 0:
                                    rx_frame = resp
                                    rtt_ms = (t_rx - t_tx) * 1000.0
                                    break
                                elif delta > 0:
                                    logger.warning(
                                        "Frame loss detected: dropped %d packets (expected %d, got %d)",
                                        delta,
                                        self.expected_rx_seq,
                                        rx_seq,
                                    )
                                    self.expected_rx_seq = rx_seq
                                    rx_frame = resp
                                    rtt_ms = (t_rx - t_tx) * 1000.0
                                    break
                                else:
                                    if delta == -1:
                                        logger.debug(
                                            "Stale frame drained: seq %d (window: %d)",
                                            rx_seq,
                                            self.expected_rx_seq,
                                        )
                                    else:
                                        logger.warning(
                                            "Out-of-order frame anomaly discarded: seq %d (window: %d, delta: %d)",
                                            rx_seq,
                                            self.expected_rx_seq,
                                            delta,
                                        )
                                    continue
                            except TimeoutError:
                                break

                        if rx_frame is not None:
                            raw_pv = float(rx_frame["pv"])
                            raw_dist = rx_frame.get("dist_cm")
                            height_val = rx_frame.get("height_cm")
                            tach_val = float(rx_frame.get("tach_rpm", -1.0))
                            tach_vld = bool(rx_frame.get("tach_valid", False))

                            # Reject transient zero-drops during active flight
                            if raw_pv == 0.0 and self.last_known_pv >= 40.0:
                                is_loss = True
                            else:
                                current_pv = raw_pv
                                self.last_known_pv = current_pv
                                is_loss = False
                        else:
                            is_loss = True
                            rtt_ms = None

                        if self.expected_rx_seq is not None:
                            self.expected_rx_seq += 1
                    else:
                        is_loss = True
                except (
                    OSError,
                    ConnectionRefusedError,
                    json.JSONDecodeError,
                    KeyError,
                ) as exc:
                    logger.debug("Telemetry drop (tick %d): %s", self.seq_num, exc)
                    is_loss = True
                    rtt_ms = None

            # Advance Discrete Controller Dynamics (Single Step per Frame)
            if isinstance(controller, ResilientPID):
                u_t = controller.update(
                    setpoint=sp, pv_actual=current_pv, is_loss=is_loss
                )
                if is_loss:
                    self.last_known_pv = controller.y_est
            elif isinstance(controller, DiscretePID):
                u_t = controller.update(
                    setpoint=sp, pv=self.last_known_pv, is_loss=is_loss
                )
            else:
                # SmithPredictor accepts (setpoint, pv, is_loss)
                u_t = controller.update(
                    setpoint=sp, pv=self.last_known_pv, is_loss=is_loss
                )

            self.last_u = u_t
            error = sp - self.last_known_pv

            # Stream Telemetry Metrics to InfluxDB with Physical Distance and TACH
            self.telemetry.log_control_metrics(
                trial_id=trial,
                algorithm=active_mode,
                setpoint=sp,
                process_variable=self.last_known_pv,
                control_signal=u_t,
                error=error,
                rtt_ms=rtt_ms,
                dt_exec_ms=dt_exec_ms,
                is_loss=is_loss,
                raw_distance_cm=raw_dist,
                height_cm=height_val,
                fan_tach_rpm=tach_val,
                tach_valid=tach_vld,
            )

            self.seq_num += 1
            next_tick += dt
            sleep_rem = next_tick - time.perf_counter()
            if sleep_rem > 0:
                time.sleep(sleep_rem)
            else:
                next_tick = time.perf_counter()


def main() -> None:
    """CLI orchestrator entry point."""
    args = parse_args()
    c2_proc = None
    c2_log_file = None

    if not args.no_c2:
        if not is_port_in_use(args.c2_port):
            logger.info(
                "Port %d vacant. Spawning background C2 process...", args.c2_port
            )
            c2_log_file = open("/tmp/c2_server.log", "a")
            cmd = [
                sys.executable,
                "-m",
                "resilient_pid.c2.c2_server",
                "--port",
                str(args.c2_port),
            ]
            if args.enable_ui:
                cmd.append("--enable-ui")
            c2_proc = subprocess.Popen(cmd, stdout=c2_log_file, stderr=c2_log_file)
            time.sleep(1.0)
        else:
            logger.info("Existing C2 server detected on port %d.", args.c2_port)

        init_payload: dict[str, Any] = {
            "ui_enabled": args.enable_ui,
            "algorithm": args.mode,
            "is_running": True,
        }
        if args.setpoint is not None:
            init_payload["setpoint"] = args.setpoint
        if args.trial_id:
            init_payload["trial_id"] = args.trial_id

        push_c2_configuration(args.c2_port, init_payload)

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