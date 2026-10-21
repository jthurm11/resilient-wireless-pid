#!/usr/bin/env python3
"""
src/resilient_pid/plant/plant_interface.py
Unified Plant Runtime Daemon & Diagnostic Suite for Resilient Wireless PID DCS.
Provides an interchangeable interface between physical hardware (PWM/I2C/GPIO)
and a continuous aerodynamic software twin (RK4 integration).

Uses an asynchronous worker thread for HC-SR04 ping acquisition to decouple
acoustic flight-time latency from real-time UDP telemetry response deadlines.
Implements POSIX signal handling (SIGTERM/SIGINT) to guarantee safe de-energization
of the EMC2101 fan actuator and deterministic deallocation of GPIO lines.

Integrated Diagnostic Flags:
  --op-test      : Validates static sensor calibration, dynamic flight response,
                   and remote C2 network packet reaction.
  --cycle-test   : Executes multi-step mechanical cycling.
  --ignore-tach  : Bypasses zero/invalid TACH RPM gates during diagnostics.
"""

import argparse
import json
import logging
import signal
import socket
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from typing import Any

import numpy as np

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("PlantInterface")

# Canonical Column Dimensional Mapping
TUBE_GEOMETRY_DEFAULTS = {
    "acrylic": {"length_cm": 45.085, "id_mm": 51.0},
    "cardboard": {"length_cm": 78.0, "id_mm": 50.8},
}

# Conditional import of physical hardware drivers
smbus2: Any = None
GPIO: Any = None
HARDWARE_AVAILABLE: bool = False

try:
    import RPi.GPIO as _GPIO
    import smbus2 as _smbus2

    GPIO = _GPIO
    smbus2 = _smbus2
    HARDWARE_AVAILABLE = True
except (ImportError, RuntimeError):
    pass


class BasePlant(ABC):
    """Abstract Base Class enforcing the DCS physical process contract."""

    @abstractmethod
    def step(self, u_t: float) -> float:
        """
        Accepts the commanded control effort u(t) and advances plant dynamics.

        :param u_t: Control input [0.0, 100.0]%
        :return: Current process variable feedback pv(t) [0.0, 100.0]%
        """
        pass

    @abstractmethod
    def cleanup(self) -> None:
        """Safely de-energize physical actuators or release driver handles."""
        pass


class SimulatedPlant(BasePlant):
    """
    High-fidelity continuous aerodynamic twin of the PingPongPID testbed.
    Models DC blower rotational inertia, non-linear fluid drag relative to
    flow speed, gravitational forces, tube-wall boundary dissipation,
    and acoustic sensor quantization noise. Integrated via Runge-Kutta 4 (RK4).
    """

    def __init__(
        self,
        dt: float = 0.05,
        tube_length_m: float = 0.45085,  # 45.085 cm acrylic column
        tube_type: str = "acrylic",
        ball_mass_kg: float = 0.0027,  # 2.7g standard ping-pong ball
        ball_radius_m: float = 0.020,  # 40mm diameter
        g: float = 9.80665,
    ):
        self.dt = dt
        self.L_tube = tube_length_m
        self.tube_type = tube_type
        self.m = ball_mass_kg
        self.r = ball_radius_m
        self.g = g

        # Physical constants
        self.rho = 1.204  # Air density at 20°C (kg/m^3)
        self.cd = 0.47  # Sphere drag coefficient
        self.area = np.pi * (self.r**2)

        # Actuator curve: 9.5 m/s max airflow gives hover at ~48-52% PWM
        self.tau_fan = 0.18  # Rotational electromechanical time constant (s)
        self.v_air_max = 12.5  # Max steady-state airspeed (m/s)

        # State: [v_air (m/s), y_pos (m), v_ball (m/s)]
        self.state = np.array([0.0, 0.0, 0.0], dtype=np.float64)

        # Mechanical boundaries
        self.cor_bottom = 0.20
        self.cor_top = 0.25
        self.friction_coeff = 0.05

        logger.info(
            "SimulatedPlant online: Column: %s (L=%.2f cm) | Solver: RK4 (dt=%.3fs) | "
            "Aero: rho=%.3f kg/m^3, Cd=%.2f, m=%.1fg | Vmax=%.1f m/s (tau=%.2fs)",
            self.tube_type.upper(),
            self.L_tube * 100.0,
            self.dt,
            self.rho,
            self.cd,
            self.m * 1000.0,
            self.v_air_max,
            self.tau_fan,
        )

    def _dynamics(
        self, state: np.ndarray, u_clamped: float, turbulent_flow: float
    ) -> np.ndarray:
        v_air, y, v_ball = state

        # Blower lag ODE: tau * dv_air/dt = target - v_air
        target_v_air = ((u_clamped / 100.0) ** 1.1) * self.v_air_max
        d_v_air = (target_v_air - v_air) / self.tau_fan

        # Annular exhaust pressure drop along the tube height
        gradient = 1.0 - 0.20 * (y / self.L_tube)
        net_air_speed = max(0.0, (v_air + turbulent_flow) * gradient)

        # Relative fluid velocity across the sphere surface
        v_rel = net_air_speed - v_ball
        f_aero = 0.5 * self.rho * self.cd * self.area * v_rel * abs(v_rel)

        # Wall dissipation & gravity
        f_wall = self.friction_coeff * v_ball
        accel = (f_aero / self.m) - self.g - f_wall

        # Mechanical floor stop constraint
        if y <= 0.0 and accel <= 0.0:
            accel = 0.0
            v_ball = 0.0
            d_v_pos = 0.0
        else:
            d_v_pos = v_ball

        return np.array([d_v_air, d_v_pos, accel], dtype=np.float64)

    def step(self, u_t: float) -> float:
        u_clamped = float(np.clip(u_t, 0.0, 100.0))

        # Dynamic vortex shedding perturbation
        turbulence_sigma = 0.25 * (self.state[0] / self.v_air_max)
        turbulent_flow = float(np.random.normal(0.0, max(0.01, turbulence_sigma)))

        # Runge-Kutta 4 Numerical Integration
        k1 = self._dynamics(self.state, u_clamped, turbulent_flow)
        k2 = self._dynamics(self.state + 0.5 * self.dt * k1, u_clamped, turbulent_flow)
        k3 = self._dynamics(self.state + 0.5 * self.dt * k2, u_clamped, turbulent_flow)
        k4 = self._dynamics(self.state + self.dt * k3, u_clamped, turbulent_flow)

        self.state += (self.dt / 6.0) * (k1 + 2.0 * k2 + 2.0 * k3 + k4)

        # Enforce physical mechanical hard-stops
        if self.state[1] <= 0.0:
            self.state[1] = 0.0
            if self.state[2] < 0.0:
                self.state[2] = -self.state[2] * self.cor_bottom
                if abs(self.state[2]) < 0.03:
                    self.state[2] = 0.0

        elif self.state[1] >= self.L_tube:
            self.state[1] = self.L_tube
            if self.state[2] > 0.0:
                self.state[2] = -self.state[2] * self.cor_top
                if abs(self.state[2]) < 0.03:
                    self.state[2] = 0.0

        # Map to percentage [0.0, 100.0]%
        pv_true = (self.state[1] / self.L_tube) * 100.0

        # Transducer acoustic reflection noise (0.25% variance)
        sensor_noise = np.random.normal(0.0, 0.25)
        return float(np.clip(pv_true + sensor_noise, 0.0, 100.0))

    def cleanup(self) -> None:
        logger.info("Cleaning up simulated plant state.")


class HardwarePlant(BasePlant):
    """
    Physical hardware interface running on bare-metal Raspberry Pi.
    Actuates a 12V 4-wire fan via EMC2101 (I2C 0x4C) and samples
    ball elevation via an asynchronous HC-SR04 ultrasonic worker.
    """

    # EMC2101 Register Map
    REG_CONFIG = 0x03
    REG_STATUS = 0x02
    REG_TACH_LSB = 0x46
    REG_TACH_MSB = 0x47
    REG_FAN_CONFIG = 0x4A
    REG_FAN_SPINUP = 0x4B
    REG_FAN_SETTING = 0x4C
    REG_PWM_FREQ = 0x4D
    REG_PWM_DIV = 0x4E

    def __init__(
        self,
        i2c_bus: int = 1,
        emc2101_addr: int = 0x4C,
        trig_pin: int = 23,
        echo_pin: int = 24,
        tube_length_cm: float = 45.72,
        tube_type: str = "acrylic",
        sensor_at_top: bool = True,
        enable_sensor_thread: bool = True,
    ):
        if not HARDWARE_AVAILABLE:
            raise RuntimeError(
                "Hardware drivers missing. Run only on physical Raspberry Pi nodes."
            )

        self.emc_addr = emc2101_addr
        self.trig_pin = trig_pin
        self.echo_pin = echo_pin
        self.tube_length_cm = tube_length_cm
        self.tube_type = tube_type
        self.sensor_at_top = sensor_at_top
        self.enable_sensor_thread = enable_sensor_thread

        # Physical Calibration Constants
        # d_min: Acoustic distance when ball apex touches top aperture (~1.5 cm)
        # d_max: Acoustic distance when ball rests on bottom fan grille (~45.5 cm)
        self.d_min_cm = 1.5
        self.d_max_cm = self.tube_length_cm

        # Thread synchronization state
        self._lock = threading.Lock()
        self._current_pv: float = 0.0
        self._last_distance_cm: float = self.d_max_cm
        self._miss_count: int = 0
        self._running: bool = True
        self._last_u_cmd: float = 0.0

        # Initialize EMC2101 over SMBus/I2C
        self.bus = smbus2.SMBus(i2c_bus)
        self._init_emc2101()

        # Initialize HC-SR04 Ultrasonic GPIO Lines
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(self.trig_pin, GPIO.OUT)
        GPIO.setup(self.echo_pin, GPIO.IN)
        GPIO.output(self.trig_pin, GPIO.LOW)
        time.sleep(0.05)  # Transducer settling interval

        # Launch Asynchronous Sensor Acquisition Worker
        if self.enable_sensor_thread:
            self.worker_thread = threading.Thread(
                target=self._sensor_worker, name="HCSR04-Poller", daemon=True
            )
            self.worker_thread.start()

        poll_mode_str = (
            "Async-Thread (~28.5Hz)" if self.enable_sensor_thread else "Synchronous"
        )
        logger.info(
            "HardwarePlant online: Column: %s (L=%.2f cm) | "
            "Transducer: HC-SR04 (BCM %d/%d, Calibration=[%.1f, %.1f] cm, %s) | "
            "Actuator: EMC2101 (0x%02X on I2C-%d, PWM=1.4kHz)",
            self.tube_type.upper(),
            self.tube_length_cm,
            self.trig_pin,
            self.echo_pin,
            self.d_min_cm,
            self.d_max_cm,
            poll_mode_str,
            self.emc_addr,
            i2c_bus,
        )

    def _init_emc2101(self) -> None:
        """Configures EMC2101 for direct PWM control and max resolution."""
        try:
            cfg = self.bus.read_byte_data(self.emc_addr, self.REG_FAN_CONFIG)
            self.bus.write_byte_data(self.emc_addr, self.REG_FAN_CONFIG, cfg | 0x20)
            self.bus.write_byte_data(self.emc_addr, self.REG_FAN_SPINUP, 0x00)
            self.bus.write_byte_data(self.emc_addr, self.REG_PWM_FREQ, 0x1F)
            self.bus.write_byte_data(self.emc_addr, self.REG_PWM_DIV, 0x01)
            self.bus.write_byte_data(self.emc_addr, self.REG_FAN_SETTING, 0x00)
        except Exception as e:
            logger.error("Failed to initialize EMC2101 over I2C: %s", e)
            raise

    def set_fan_pwm(self, duty_percent: float) -> None:
        """Sets fan duty cycle [0.0 - 100.0] via 6-bit register (0-63)."""
        clamped = max(0.0, min(100.0, duty_percent))
        self._last_u_cmd = clamped
        reg_value = int(round((clamped / 100.0) * 63.0))
        try:
            self.bus.write_byte_data(self.emc_addr, self.REG_FAN_SETTING, reg_value)
        except Exception as e:
            logger.warning("EMC2101 I2C write failed: %s", e)

    def read_tach_rpm(self) -> float:
        """Decodes raw 16-bit TACH registers to revolutions-per-minute."""
        try:
            lsb = self.bus.read_byte_data(self.emc_addr, self.REG_TACH_LSB)
            msb = self.bus.read_byte_data(self.emc_addr, self.REG_TACH_MSB)
            count = (msb << 8) | lsb
            if count in (0x0000, 0xFFFF):
                return 0.0
            return float((90000 * 60) / count)
        except Exception as e:
            logger.debug("Failed to read TACH from EMC2101: %s", e)
            return 0.0

    def get_telemetry_snapshot(self) -> tuple[float, float, int]:
        """Atomically retrieves the latest sensor process variable, distance, and miss counter."""
        with self._lock:
            return self._current_pv, self._last_distance_cm, self._miss_count

    def _acquire_distance_cm(self) -> float:
        """
        Executes a single bounded acoustic ping measurement.
        Returns distance in cm, or -1.0 if an edge times out.
        """
        # Drain residual HIGH state on ECHO from previous bounces
        t_drain_limit = time.perf_counter() + 0.003
        while GPIO.input(self.echo_pin) == 1:
            if time.perf_counter() > t_drain_limit:
                return -1.0

        # Quiet holdoff (2ms) & 10us trigger pulse
        GPIO.output(self.trig_pin, GPIO.LOW)
        time.sleep(0.002)
        GPIO.output(self.trig_pin, GPIO.HIGH)
        time.sleep(0.00001)
        GPIO.output(self.trig_pin, GPIO.LOW)

        # Wait for rising edge (bounded to 6ms)
        t_rise_deadline = time.perf_counter() + 0.006
        while GPIO.input(self.echo_pin) == 0:
            if time.perf_counter() > t_rise_deadline:
                return -1.0
        pulse_start = time.perf_counter()

        # Wait for falling edge (bounded to 10ms ~ 170cm flight limit)
        t_fall_deadline = pulse_start + 0.010
        while GPIO.input(self.echo_pin) == 1:
            if time.perf_counter() > t_fall_deadline:
                return -1.0
        pulse_end = time.perf_counter()

        duration = pulse_end - pulse_start
        return (duration * 34300.0) / 2.0

    def _sensor_worker(self) -> None:
        """Dedicated background thread polling HC-SR04 with acoustic blind-zone mitigation."""
        while self._running:
            d = self._acquire_distance_cm()

            # Retry once on miss
            if d < 0.0:
                time.sleep(0.001)
                d = self._acquire_distance_cm()

            with self._lock:
                # Blind-zone ceiling lock detection:
                # If sensor times out (d < 0) or reads extremely short distance (d < 2.0cm)
                # while the fan duty cycle is high (u > 50%), the ball is pinned at the top aperture.
                if (d < 2.0 or d < 0.0) and self._last_u_cmd > 50.0 and self._current_pv > 60.0:
                    self._miss_count = 0
                    self._last_distance_cm = self.d_min_cm
                    self._current_pv = 100.0
                elif 1.0 <= d <= (self.d_max_cm + 5.0):
                    self._miss_count = 0
                    self._last_distance_cm = d

                    # Linear normalization across calibrated physical range [d_min, d_max]
                    d_clamped = max(self.d_min_cm, min(self.d_max_cm, d))
                    if self.sensor_at_top:
                        pv_normalized = ((self.d_max_cm - d_clamped) / (self.d_max_cm - self.d_min_cm)) * 100.0
                    else:
                        pv_normalized = ((d_clamped - self.d_min_cm) / (self.d_max_cm - self.d_min_cm)) * 100.0

                    self._current_pv = float(np.clip(pv_normalized, 0.0, 100.0))
                else:
                    self._miss_count += 1
                    if self._miss_count % 20 == 0:
                        logger.warning(
                            "HC-SR04: %d consecutive missed pings (last reading: %.1f cm)",
                            self._miss_count,
                            d,
                        )

            time.sleep(0.035)

    def step(self, u_t: float) -> float:
        self.set_fan_pwm(u_t)
        pv, dist, misses = self.get_telemetry_snapshot()

        if logger.isEnabledFor(logging.DEBUG) or int(time.time() * 2) % 10 == 0:
            logger.info(
                "HC-SR04 Telemetry: dist=%.1f cm, pv=%.1f%% (mount=%s, u=%.1f%%, misses=%d)",
                dist,
                pv,
                "TOP" if self.sensor_at_top else "BOTTOM",
                u_t,
                misses,
            )

        return pv

    def cleanup(self) -> None:
        """Atomic safe de-energization of physical actuators and pin release."""
        logger.info("Executing HardwarePlant teardown sequence...")
        self._running = False
        if hasattr(self, "worker_thread") and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=0.3)

        try:
            if hasattr(self, "bus") and self.bus is not None:
                self.bus.write_byte_data(self.emc_addr, self.REG_FAN_SETTING, 0x00)
                self.bus.close()
                logger.info("EMC2101 fan duty cycle zeroed and SMBus closed.")
        except Exception as e:
            logger.error("Failed to zero fan actuator during teardown: %s", e)

        try:
            if HARDWARE_AVAILABLE and GPIO is not None:
                GPIO.cleanup([self.trig_pin, self.echo_pin])
                logger.info("GPIO lines released: TRIG=%d, ECHO=%d.", self.trig_pin, self.echo_pin)
        except Exception as e:
            logger.error("Failed to release GPIO lines: %s", e)


def ensure_service_stopped(service_name: str = "dcs-plant.service") -> None:
    """Verifies that the background systemd daemon is deactivated."""
    try:
        status_res = subprocess.run(
            ["systemctl", "is-active", "--quiet", service_name],
            check=False,
        )
        if status_res.returncode == 0:
            logger.warning(
                "%s is running. Stopping service to prevent socket/I2C contention...",
                service_name,
            )
            subprocess.run(["sudo", "systemctl", "stop", service_name], check=True)
            time.sleep(1.0)
            logger.info("%s stopped successfully.", service_name)
    except Exception as e:
        logger.error("Failed to evaluate/stop %s: %s", service_name, e)


def run_op_test(
    plant: HardwarePlant,
    host: str,
    port: int,
    max_frames: int = 50,
    ignore_tach: bool = False,
) -> None:
    """Executes structured operational testing across sensor, fan, and UDP network."""
    logger.info("=================================================================")
    logger.info("PHASE 0: Static Sensor Baseline & Tube Geometry Calibration")
    logger.info("=================================================================")

    plant.set_fan_pwm(0.0)
    time.sleep(1.0)

    pre_test_samples: list[float] = []
    logger.info("Acquiring 10 resting distance samples from HC-SR04...")
    for _ in range(10):
        _, dist, misses = plant.get_telemetry_snapshot()
        if dist > 2.0:
            pre_test_samples.append(dist)
        time.sleep(0.08)

    if not pre_test_samples:
        logger.error(
            "PHASE 0 FAILED: Ultrasonic transducer returned 0 valid acoustic reflections. "
            "Inspect HC-SR04 power and GPIO pins 23/24."
        )
        return

    baseline_dist_cm = float(np.mean(pre_test_samples))
    baseline_std_cm = float(np.std(pre_test_samples))
    logger.info(
        "Static Baseline: Distance to ball = %.2f cm (std: %.2f cm) | Configured Tube = %.1f cm",
        baseline_dist_cm,
        baseline_std_cm,
        plant.tube_length_cm,
    )

    # Sanity check: verify physical distance matches expected chamber length
    tube_delta = abs(baseline_dist_cm - plant.tube_length_cm)
    if tube_delta > 5.0:
        logger.warning(
            "Geometry Mismatch: Measured baseline (%.1f cm) deviates by %.1f cm from "
            "configured tube length (%.1f cm). Check --tube-length calibration.",
            baseline_dist_cm,
            tube_delta,
            plant.tube_length_cm,
        )
    else:
        logger.info(
            "Geometry Verified: Baseline measurement conforms to physical column calibration."
        )

    logger.info("=================================================================")
    logger.info("PHASE 1: Open-Loop Fan Step Sweep & Ball Aerodynamics Evaluation")
    if ignore_tach:
        logger.info("[NOTICE] TACH RPM validation gate bypassed via --ignore-tach.")
    logger.info("=================================================================")

    test_levels = [0.0, 30.0, 60.0, 90.0, 0.0]

    for target_pwm in test_levels:
        plant.set_fan_pwm(target_pwm)
        logger.info(
            "Commanded Duty: %5.1f%% | Allowing pneumatic stabilization (1.8s)...",
            target_pwm,
        )
        time.sleep(1.8)

        tach_samples: list[float] = []
        pv_samples: list[float] = []
        dist_samples: list[float] = []

        for _ in range(6):
            tach_samples.append(plant.read_tach_rpm())
            pv, dist, _ = plant.get_telemetry_snapshot()
            pv_samples.append(pv)
            dist_samples.append(dist)
            time.sleep(0.2)

        avg_rpm = float(np.mean(tach_samples)) if tach_samples else 0.0
        avg_pv = float(np.mean(pv_samples)) if pv_samples else 0.0
        avg_dist = float(np.mean(dist_samples)) if dist_samples else 0.0
        delta_lift_cm = baseline_dist_cm - avg_dist

        tach_str = (
            f"{avg_rpm:6.1f} RPM" if avg_rpm > 0.0 else "N/A (Zero Tach Feedback)"
        )
        logger.info(
            "Output: Command: %5.1f%% | PV: %5.1f%% | Dist: %5.1f cm (Lift: %+5.1f cm) | TACH: %s",
            target_pwm,
            avg_pv,
            avg_dist,
            delta_lift_cm,
            tach_str,
        )

        if target_pwm >= 60.0:
            if delta_lift_cm < 5.0 and avg_pv < 10.0:
                logger.warning(
                    "Aerodynamic Inefficiency: Fan commanded to %.1f%% but ball lift is only %.1f cm. "
                    "Verify 12V rail power to fan and plenum seals.",
                    target_pwm,
                    delta_lift_cm,
                )
            else:
                logger.info(
                    "Pneumatic Actuation Verified: Ball displaced by +%.1f cm at %.1f%% PWM.",
                    delta_lift_cm,
                    target_pwm,
                )
        elif target_pwm == 0.0 and avg_pv > 15.0:
            logger.warning(
                "Resting Offset: Commanded 0%% PWM but ball remains elevated (PV: %.1f%%). Ball may be stuck.",
                avg_pv,
            )

        if target_pwm >= 30.0 and avg_rpm < 100.0:
            if not ignore_tach:
                logger.error(
                    "PHASE 1 FAILED: Commanded %.1f%% PWM but TACH reported %.1f RPM.",
                    target_pwm,
                    avg_rpm,
                )
                return
            else:
                logger.warning(
                    "Zero TACH reading ignored at %.1f%% PWM due to --ignore-tach.",
                    target_pwm,
                )

    logger.info(
        "PHASE 1 COMPLETE: Actuation sweep and physical aerodynamic response confirmed."
    )

    logger.info("=================================================================")
    logger.info("PHASE 2: Remote Control Node Message Reaction Test")
    logger.info(
        "Binding UDP %s:%d to process %d incoming controller datagrams...",
        host,
        port,
        max_frames,
    )
    logger.info("=================================================================")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.settimeout(6.0)

    received = 0
    try:
        while received < max_frames:
            try:
                data, addr = sock.recvfrom(1024)
                t_recv = time.perf_counter()
                pkt = json.loads(data.decode("utf-8"))

                seq = pkt.get("seq")
                u_cmd = float(pkt.get("u", 0.0))
                t_send = pkt.get("t_send")

                pv = plant.step(u_cmd)
                rpm = plant.read_tach_rpm()

                if t_send and t_send > 1e9:
                    epoch_now = time.time()
                    transit_ms = (epoch_now - t_send) * 1000.0
                    transit_str = f"{transit_ms:6.2f} ms (OWD)"
                else:
                    transit_str = "N/A (Monotonic Skew)"

                tach_str = f"{rpm:6.1f} RPM" if rpm > 0.0 else "N/A"

                logger.info(
                    "[RX #%02d] Seq: %s | Command u: %5.1f%% | PV: %5.1f%% | TACH: %s | RTT: %s",
                    received + 1,
                    seq,
                    u_cmd,
                    pv,
                    tach_str,
                    transit_str,
                )

                resp = {"seq": seq, "pv": pv, "t_send": t_send, "t_echo": t_recv}
                sock.sendto(json.dumps(resp).encode("utf-8"), addr)
                received += 1

            except TimeoutError:
                logger.error(
                    "Socket timeout (>6s). No datagrams received from control node."
                )
                return
    finally:
        sock.close()

    logger.info(
        "PHASE 2 COMPLETE: Successfully handled %d closed-loop controller frames.",
        received,
    )


def run_cycle_test(
    plant: HardwarePlant, cycles: int = 5, step_interval: float = 3.0
) -> None:
    """Cycles the fan through step profiles to test mechanical repeatability."""
    logger.info("=================================================================")
    logger.info("Executing Continuous Mechanical Cycle Test (%d iterations)", cycles)
    logger.info("=================================================================")

    duty_profile = [20.0, 50.0, 80.0, 40.0, 0.0]

    for cycle in range(1, cycles + 1):
        logger.info("--- Starting Cycle %d of %d ---", cycle, cycles)
        for u in duty_profile:
            pv = plant.step(u)
            time.sleep(step_interval)
            rpm = plant.read_tach_rpm()
            _, dist, _ = plant.get_telemetry_snapshot()
            tach_str = f"{rpm:6.1f} RPM" if rpm > 0.0 else "N/A"
            logger.info(
                "Command: %5.1f%% | PV: %5.1f%% | Dist: %5.1f cm | TACH: %s",
                u,
                pv,
                dist,
                tach_str,
            )

    logger.info("Continuous cycle testing complete.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Distributed Control System - Plant Node Daemon"
    )
    parser.add_argument("--mode", choices=["hardware", "simulate"], default="hardware")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5005)
    parser.add_argument(
        "--sensor-at-top",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Whether ultrasonic sensor is top-mounted pointing down (default: True).",
    )
    parser.add_argument(
        "--tube-type",
        choices=["acrylic", "cardboard"],
        default="acrylic",
        help="Physical column variant installed on the testbed (default: acrylic).",
    )
    parser.add_argument(
        "--tube-length",
        type=float,
        default=None,
        help="Calibrated physical column height in cm. If omitted, automatically resolves "
        "to the default for --tube-type (acrylic: 45.085 cm, cardboard: 78.0 cm).",
    )

    # Diagnostic mode arguments
    diag_group = parser.add_mutually_exclusive_group()
    diag_group.add_argument(
        "--op-test",
        action="store_true",
        help="Execute operational fan actuation and remote controller message verification.",
    )
    diag_group.add_argument(
        "--cycle-test",
        action="store_true",
        help="Execute continuous dynamic actuation step-cycle test.",
    )
    parser.add_argument(
        "--ignore-tach",
        action="store_true",
        help="Ignore zero/bad TACH readings during diagnostic execution without aborting.",
    )
    parser.add_argument(
        "--cycles",
        type=int,
        default=5,
        help="Number of iterations for --cycle-test (default: 5).",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # Pre-execution service contention handling
    if args.op_test or args.cycle_test:
        ensure_service_stopped("dcs-plant.service")

    # Resolve geometric column length based on tube type or explicit override
    if args.tube_length is not None:
        effective_tube_length_cm = args.tube_length
    else:
        effective_tube_length_cm = TUBE_GEOMETRY_DEFAULTS.get(
            args.tube_type, TUBE_GEOMETRY_DEFAULTS["acrylic"]
        )["length_cm"]

    plant: BasePlant
    if args.mode == "hardware":
        if not HARDWARE_AVAILABLE:
            logger.error(
                "Physical drivers unavailable. Falling back to simulated twin."
            )
            plant = SimulatedPlant(
                tube_length_m=effective_tube_length_cm / 100.0,
                tube_type=args.tube_type,
            )
        else:
            plant = HardwarePlant(
                tube_length_cm=effective_tube_length_cm,
                tube_type=args.tube_type,
                sensor_at_top=args.sensor_at_top,
            )
    else:
        plant = SimulatedPlant(
            tube_length_m=effective_tube_length_cm / 100.0,
            tube_type=args.tube_type,
        )

    # Dispatch to diagnostics if requested
    if args.op_test:
        if not isinstance(plant, HardwarePlant):
            logger.fatal("--op-test requires physical hardware execution.")
            sys.exit(1)
        try:
            run_op_test(
                plant,
                host=args.host,
                port=args.port,
                ignore_tach=args.ignore_tach,
            )
        finally:
            plant.cleanup()
        return

    if args.cycle_test:
        if not isinstance(plant, HardwarePlant):
            logger.fatal("--cycle-test requires physical hardware execution.")
            sys.exit(1)
        try:
            run_cycle_test(plant, cycles=args.cycles)
        finally:
            plant.cleanup()
        return

    # Nominal Daemon Runtime Loop
    logger.info(
        "Plant Service active | Mode: %s | Socket: %s:%d | Mount: %s",
        args.mode.upper(),
        args.host,
        args.port,
        "TOP" if args.sensor_at_top else "BOTTOM",
    )

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.host, args.port))

    shutdown_requested = threading.Event()

    def _sig_handler(signum: int, _frame: Any) -> None:
        """Intercepts SIGTERM and SIGINT to cleanly unwind the primary UDP thread."""
        sig_name = signal.Signals(signum).name
        logger.info(
            "Signal %s (%d) caught. Unwinding main runtime loop...", sig_name, signum
        )
        shutdown_requested.set()
        try:
            sock.close()
        except OSError:
            pass

    signal.signal(signal.SIGTERM, _sig_handler)
    signal.signal(signal.SIGINT, _sig_handler)

    try:
        while not shutdown_requested.is_set():
            try:
                data, addr = sock.recvfrom(1024)
            except OSError:
                break

            t_recv = time.perf_counter()

            try:
                pkt = json.loads(data.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue

            seq = pkt.get("seq")
            u_t = float(pkt.get("u", 0.0))
            t_send = pkt.get("t_send")

            pv = plant.step(u_t)

            resp = {"seq": seq, "pv": pv, "t_send": t_send, "t_echo": t_recv}
            try:
                sock.sendto(json.dumps(resp).encode("utf-8"), addr)
            except OSError:
                break

    except KeyboardInterrupt:
        logger.info("Interactive KeyboardInterrupt caught.")
    finally:
        logger.info("Entering final daemon shutdown sequence...")
        plant.cleanup()
        try:
            sock.close()
        except OSError:
            pass
        logger.info("Plant service successfully stopped.")


if __name__ == "__main__":
    main()
