#!/usr/bin/env python3
"""
src/resilient_pid/plant/plant_interface.py
Unified Plant Runtime Daemon & Diagnostic Suite for Resilient Wireless PID DCS.
Provides an interchangeable interface between physical hardware (PWM/I2C/GPIO)
and a continuous aerodynamic software twin (RK4 integration).

Uses asynchronous worker threads for HC-SR04 ping acquisition and EMC2101 TACH polling
to decouple physical sensor flight time and I2C SMBus latency from real-time UDP deadlines.
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
    def get_telemetry_snapshot(self) -> tuple[float, float, int, float, bool]:
        """
        Atomically returns (current_pv_percent, raw_distance_cm, miss_count, tach_rpm, tach_valid).
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
        self.cd = 0.62  # Calibrated drag coefficient for acrylic tube
        self.area = np.pi * (self.r**2)

        # Calibrated Actuator Dynamic Curve:
        # Vmax = 21.5 m/s ensures dynamic liftoff reliably clears the PV >= 16% disarm gate
        # while matching the testbed hover baseline at ~34% - 37% PWM
        self.tau_fan = 0.18  # Rotational electromechanical time constant (s)
        self.v_air_max = 21.5  # Max steady-state airspeed (m/s)

        # Dynamic state vector: [v_air (m/s), y_pos (m), v_ball (m/s)]
        self.state = np.array([0.0, 0.0, 0.0], dtype=np.float64)
        self._current_pv: float = 0.0
        self._last_distance_cm: float = self.L_tube * 100.0
        self._sim_rpm: float = 0.0

        # Mechanical boundaries
        self.cor_bottom = 0.08
        self.cor_top = 0.10
        # Calibrated air column shear layer dissipation to suppress limit-cycle hunting
        self.friction_coeff = 0.095

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

        # Blower electromechanical lag ODE: tau * dv_air/dt = target - v_air
        target_v_air = ((u_clamped / 100.0) ** 1.1) * self.v_air_max
        d_v_air = (target_v_air - v_air) / self.tau_fan

        # Annular exhaust pressure drop along tube height
        gradient = 1.0 - 0.20 * (y / self.L_tube)
        net_air_speed = max(0.0, (v_air + turbulent_flow) * gradient)

        # Relative fluid velocity across sphere surface
        v_rel = net_air_speed - v_ball
        f_aero = 0.5 * self.rho * self.cd * self.area * v_rel * abs(v_rel)

        # Wall dissipation opposing velocity
        f_wall = self.friction_coeff * v_ball

        # Vertical acceleration
        accel = (f_aero / self.m) - self.g - (f_wall / self.m)

        # Kinematic bottom stop constraint (wire mesh reaction)
        if y <= 0.0 and accel <= 0.0:
            accel = 0.0
            v_ball = 0.0
            d_v_pos = 0.0
        else:
            d_v_pos = v_ball

        return np.array([d_v_air, d_v_pos, accel], dtype=np.float64)

    def step(self, u_t: float) -> float:
        u_clamped = float(np.clip(u_t, 0.0, 100.0))
        self._sim_rpm = (u_clamped / 100.0) * 3200.0

        # Dynamic vortex shedding perturbation
        turbulence_sigma = 0.05 * (self.state[0] / self.v_air_max)
        turbulent_flow = float(np.random.normal(0.0, max(0.005, turbulence_sigma)))

        # Runge-Kutta 4 Integration
        k1 = self._dynamics(self.state, u_clamped, turbulent_flow)
        k2 = self._dynamics(self.state + 0.5 * self.dt * k1, u_clamped, turbulent_flow)
        k3 = self._dynamics(self.state + 0.5 * self.dt * k2, u_clamped, turbulent_flow)
        k4 = self._dynamics(self.state + self.dt * k3, u_clamped, turbulent_flow)

        self.state += (self.dt / 6.0) * (k1 + 2.0 * k2 + 2.0 * k3 + k4)

        # Boundary collision constraints
        if self.state[1] <= 0.0:
            self.state[1] = 0.0
            if self.state[2] < 0.0:
                self.state[2] = -self.state[2] * self.cor_bottom
                if abs(self.state[2]) < 0.02:
                    self.state[2] = 0.0

        elif self.state[1] >= self.L_tube:
            self.state[1] = self.L_tube
            if self.state[2] > 0.0:
                self.state[2] = -self.state[2] * self.cor_top
                if abs(self.state[2]) < 0.02:
                    self.state[2] = 0.0

        # Percentage mapping [0.0, 100.0]%
        pv_true = (self.state[1] / self.L_tube) * 100.0

        # Transducer acoustic reflection noise (0.25% variance)
        sensor_noise = np.random.normal(0.0, 0.25)
        self._current_pv = float(np.clip(pv_true + sensor_noise, 0.0, 100.0))
        self._last_distance_cm = max(0.0, (self.L_tube - self.state[1]) * 100.0)
        return self._current_pv

    def get_telemetry_snapshot(self) -> tuple[float, float, int, float, bool]:
        return self._current_pv, self._last_distance_cm, 0, self._sim_rpm, True

    def cleanup(self) -> None:
        logger.info("Cleaning up simulated plant state.")


class HardwarePlant(BasePlant):
    """
    Physical hardware interface running on bare-metal Raspberry Pi.
    Actuates a 12V 4-wire fan via EMC2101 (I2C 0x4C) and samples
    ball elevation via an asynchronous HC-SR04 ultrasonic worker.
    Decouples SMBus TACH reads into a non-blocking background thread.
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
        tube_length_cm: float = 45.085,
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

        # Calibrated Acoustic Envelope for Acrylic Column:
        # - d_min_cm: Minimum acoustic line-of-sight to top aperture stop (~1.5 cm)
        # - d_max_cm: Measured acoustic apex distance to resting sphere on wire mesh (~42.4 cm)
        self.d_min_cm = 1.5
        self.d_max_cm = 42.4 if tube_type == "acrylic" else self.tube_length_cm

        # Thread synchronization state
        self._lock = threading.Lock()
        self._current_pv: float = 0.0
        self._last_distance_cm: float = self.d_max_cm
        self._miss_count: int = 0
        self._running: bool = True
        self._last_u_cmd: float = 0.0

        # Asynchronous TACH state
        self._tach_rpm: float = -1.0
        self._tach_valid: bool = False

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

        # Launch Asynchronous Non-Blocking TACH Acquisition Worker
        self.tach_worker_thread = threading.Thread(
            target=self._tach_worker, name="EMC2101-TACH-Poller", daemon=True
        )
        self.tach_worker_thread.start()

        logger.info(
            "HardwarePlant online: Column: %s (L=%.2f cm) | "
            "Transducer: HC-SR04 (BCM %d/%d, Calibration=[%.1f, %.1f] cm) | "
            "Actuator: EMC2101 (0x%02X on I2C-%d, PWM=1.4kHz)",
            self.tube_type.upper(),
            self.tube_length_cm,
            self.trig_pin,
            self.echo_pin,
            self.d_min_cm,
            self.d_max_cm,
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

    def _tach_worker(self) -> None:
        """Background thread polling EMC2101 TACH registers at 10 Hz without blocking UDP deadlines."""
        while self._running:
            try:
                lsb = self.bus.read_byte_data(self.emc_addr, self.REG_TACH_LSB)
                msb = self.bus.read_byte_data(self.emc_addr, self.REG_TACH_MSB)
                count = (msb << 8) | lsb
                if count in (0x0000, 0xFFFF):
                    if self._last_u_cmd < 5.0:
                        rpm = 0.0
                        valid = True
                    else:
                        rpm = -1.0
                        valid = False
                else:
                    rpm = float((90000 * 60) / count)
                    valid = True
            except Exception:
                rpm = -1.0
                valid = False

            with self._lock:
                self._tach_rpm = rpm
                self._tach_valid = valid

            time.sleep(0.1)

    def read_tach_rpm(self) -> float:
        """Returns the most recent non-blocking TACH RPM reading."""
        with self._lock:
            return self._tach_rpm if self._tach_valid else 0.0

    def get_telemetry_snapshot(self) -> tuple[float, float, int, float, bool]:
        """Atomically retrieves the latest sensor process variable, distance, miss counter, and TACH state."""
        with self._lock:
            return (
                self._current_pv,
                self._last_distance_cm,
                self._miss_count,
                self._tach_rpm,
                self._tach_valid,
            )

    def _acquire_distance_cm(self) -> float:
        """
        Executes a single bounded acoustic ping measurement with CPU-safe yields.
        Returns distance in cm, or -1.0 if an edge times out.
        """
        # Drain residual HIGH state on ECHO from previous bounces
        t_drain_limit = time.perf_counter() + 0.004
        while GPIO.input(self.echo_pin) == 1:
            if time.perf_counter() > t_drain_limit:
                # Force-clear transducer line latch by cycling trigger
                GPIO.output(self.trig_pin, GPIO.LOW)
                return -1.0
            time.sleep(0.0001)  # Micro-yield prevents 100% CPU lockup

        # Quiet holdoff (2ms) & 10us trigger pulse
        GPIO.output(self.trig_pin, GPIO.LOW)
        time.sleep(0.002)
        GPIO.output(self.trig_pin, GPIO.HIGH)
        time.sleep(0.00001)
        GPIO.output(self.trig_pin, GPIO.LOW)

        # Wait for rising edge (bounded to 10ms to accommodate Linux task scheduler preemption)
        t_rise_deadline = time.perf_counter() + 0.010
        while GPIO.input(self.echo_pin) == 0:
            if time.perf_counter() > t_rise_deadline:
                return -1.0

        pulse_start = time.perf_counter()

        # Wait for falling edge (bounded to 15ms ~ 250cm acoustic horizon)
        t_fall_deadline = pulse_start + 0.015
        while GPIO.input(self.echo_pin) == 1:
            if time.perf_counter() > t_fall_deadline:
                return -1.0

        pulse_end = time.perf_counter()
        duration = pulse_end - pulse_start
        return (duration * 34300.0) / 2.0

    def _sensor_worker(self) -> None:
        """Asynchronous HC-SR04 poller with backoff protection against acoustic latching."""
        consecutive_timeouts = 0

        while self._running:
            d = self._acquire_distance_cm()

            # Bounded retry on miss
            if d < 0.0:
                consecutive_timeouts += 1
                # If transducer is timing out, allow membrane oscillation to decay
                if consecutive_timeouts > 2:
                    time.sleep(0.050)  # 50ms transducer decay holdoff
                else:
                    time.sleep(0.005)
                d = self._acquire_distance_cm()
            else:
                consecutive_timeouts = 0

            with self._lock:
                # Blind-Zone / Top Aperture Occlusion:
                # When ball is >= 70% and sensor times out or reads < 3.8 cm
                if (0.0 <= d < 3.8) or (d < 0.0 and self._current_pv >= 70.0):
                    self._miss_count = 0
                    self._last_distance_cm = self.d_min_cm
                    self._current_pv = 100.0

                # Valid Acoustic Line-of-Sight Window
                elif 3.8 <= d <= (self.d_max_cm + 5.0):
                    self._miss_count = 0
                    self._last_distance_cm = d
                    d_clamped = max(self.d_min_cm, min(self.d_max_cm, d))

                    if self.sensor_at_top:
                        pv_norm = (
                            (self.d_max_cm - d_clamped)
                            / (self.d_max_cm - self.d_min_cm)
                        ) * 100.0
                    else:
                        pv_norm = (
                            (d_clamped - self.d_min_cm)
                            / (self.d_max_cm - self.d_min_cm)
                        ) * 100.0

                    self._current_pv = float(np.clip(pv_norm, 0.0, 100.0))

                # Echo Dropout / Reflection Skew
                else:
                    self._miss_count += 1
                    if self._last_u_cmd < 5.0 and self._miss_count > 10:
                        self._current_pv = 0.0

            time.sleep(0.035)

    def step(self, u_t: float) -> float:
        self.set_fan_pwm(u_t)
        pv, dist, misses, _, _ = self.get_telemetry_snapshot()

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
        if hasattr(self, "tach_worker_thread") and self.tach_worker_thread.is_alive():
            self.tach_worker_thread.join(timeout=0.3)

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
                logger.info(
                    "GPIO lines released: TRIG=%d, ECHO=%d.",
                    self.trig_pin,
                    self.echo_pin,
                )
        except Exception as e:
            logger.error("Failed to release GPIO lines: %s", e)


def ensure_service_stopped(service_name: str = "dcs-plant.service") -> bool:
    """
    Verifies background systemd daemon status and stops it to prevent hardware/socket contention.
    Returns True if the service was actively running prior to deactivation; False otherwise.
    """
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
            return True
    except Exception as e:
        logger.error("Failed to evaluate/stop %s: %s", service_name, e)
    return False


def restore_service(
    was_active: bool,
    force_restart: bool = False,
    suppress_restart: bool = False,
    service_name: str = "dcs-plant.service",
) -> None:
    """
    Restores or initializes background systemd service according to execution policies:
      - If suppress_restart is True, no action is taken.
      - If force_restart is True, the service is started regardless of initial state.
      - If was_active is True and not suppressed, the service is restored.
    """
    if suppress_restart:
        logger.info("Service restart suppressed via --no-service-restart.")
        return
    should_start = force_restart or was_active
    if not should_start:
        logger.debug(
            "Service %s was inactive prior to test run; skipping restart.", service_name
        )
        return
    logger.info("Initiating restart of background daemon: %s...", service_name)
    try:
        subprocess.run(["sudo", "systemctl", "start", service_name], check=True)
        time.sleep(1.0)
        logger.info("%s successfully restarted and operational.", service_name)
    except Exception as e:
        logger.error("Failed to start %s: %s", service_name, e)


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
        _, dist, _, _, _ = plant.get_telemetry_snapshot()
        if dist > 2.0:
            pre_test_samples.append(dist)
        time.sleep(0.08)

    if not pre_test_samples:
        logger.error("Transducer returned 0 valid acoustic reflections.")
        return

    baseline_dist_cm = float(np.mean(pre_test_samples))
    baseline_std_cm = float(np.std(pre_test_samples))
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
            "Geometry Verified: Distance to ball = %.2f cm (std: %.2f cm) | Configured Tube = %.1f cm",
        baseline_dist_cm,
        baseline_std_cm,
        plant.tube_length_cm,
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
            pv, dist, _, _, _ = plant.get_telemetry_snapshot()
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
                logger.error("Datagram timeout (>6s).")
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
            _, dist, _, _, _ = plant.get_telemetry_snapshot()
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
    # Service lifecycle management flags
    service_group = parser.add_mutually_exclusive_group()
    service_group.add_argument(
        "--no-service-restart",
        action="store_true",
        dest="no_service_restart",
        help="Do not restart dcs-plant.service after test completion, even if active beforehand.",
    )
    service_group.add_argument(
        "--service-restart",
        action="store_true",
        dest="service_restart",
        help="Unconditionally start/restart dcs-plant.service after test completion.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # Pre-execution service contention handling
    service_was_active = False
    if args.op_test or args.cycle_test:
        service_was_active = ensure_service_stopped("dcs-plant.service")

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
        test_success = False
        try:
            run_op_test(
                plant,
                host=args.host,
                port=args.port,
                ignore_tach=args.ignore_tach,
            )
            test_success = True
        finally:
            plant.cleanup()
            if test_success:
                restore_service(
                    was_active=service_was_active,
                    force_restart=args.service_restart,
                    suppress_restart=args.no_service_restart,
                )
        return

    if args.cycle_test:
        if not isinstance(plant, HardwarePlant):
            logger.fatal("--cycle-test requires physical hardware execution.")
            sys.exit(1)
        test_success = False
        try:
            run_cycle_test(plant, cycles=args.cycles)
            test_success = True
        finally:
            plant.cleanup()
            if test_success:
                restore_service(
                    was_active=service_was_active,
                    force_restart=args.service_restart,
                    suppress_restart=args.no_service_restart,
                )
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
            _, raw_dist_cm, _, tach_rpm, tach_valid = plant.get_telemetry_snapshot()
            height_cm = (
                effective_tube_length_cm - raw_dist_cm
                if args.sensor_at_top
                else raw_dist_cm
            )

            # Augmented UDP payload with distance, height, and TACH metrics
            resp = {
                "seq": seq,
                "pv": round(pv, 2),
                "dist_cm": round(raw_dist_cm, 2),
                "height_cm": round(max(0.0, height_cm), 2),
                "tach_rpm": round(tach_rpm, 1),
                "tach_valid": bool(tach_valid),
                "t_send": t_send,
                "t_echo": t_recv,
            }
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
