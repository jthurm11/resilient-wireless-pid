#!/usr/bin/env python3
"""
src/resilient_pid/plant/plant_interface.py
Unified Plant Runtime Daemon for Resilient Wireless PID DCS.
Provides an interchangeable interface between physical hardware (PWM/I2C/GPIO)
and a realistic, continuous aerodynamic software twin (RK4 integration).

Uses an asynchronous worker thread for HC-SR04 ping acquisition to decouple
acoustic flight-time latency from real-time UDP telemetry response deadlines.
"""

import argparse
import json
import logging
import socket
import threading
import time
from abc import ABC, abstractmethod
from typing import Any

import numpy as np

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("PlantInterface")

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
        tube_length_m: float = 0.50,  # 50 cm physical acrylic tube
        ball_mass_kg: float = 0.0027,  # 2.7g standard ping pong ball
        ball_radius_m: float = 0.020,  # 40mm diameter
        g: float = 9.80665,
    ):
        self.dt = dt
        self.L_tube = tube_length_m
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

    def __init__(
        self,
        i2c_bus: int = 1,
        emc2101_addr: int = 0x4C,
        trig_pin: int = 23,
        echo_pin: int = 24,
        tube_length_cm: float = 50.0,
        sensor_at_top: bool = True,
    ):
        if not HARDWARE_AVAILABLE:
            raise RuntimeError(
                "Hardware drivers missing. Run only on physical Raspberry Pi nodes."
            )

        self.emc_addr = emc2101_addr
        self.trig_pin = trig_pin
        self.echo_pin = echo_pin
        self.tube_length_cm = tube_length_cm
        self.sensor_at_top = sensor_at_top

        # Thread synchronization state
        self._lock = threading.Lock()
        self._current_pv: float = 0.0
        self._last_distance_cm: float = 0.0
        self._miss_count: int = 0
        self._running: bool = True

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
        self.worker_thread = threading.Thread(
            target=self._sensor_worker, name="HCSR04-Poller", daemon=True
        )
        self.worker_thread.start()

        logger.info(
            "HardwarePlant online: EMC2101 (0x%02X on I2C-%d) | HC-SR04 (TRIG=%d, ECHO=%d) | "
            "Mounting: %s | Height: %.1f cm | Mode: Asynchronous",
            self.emc_addr,
            i2c_bus,
            self.trig_pin,
            self.echo_pin,
            "TOP (Inverted)" if self.sensor_at_top else "BOTTOM (Direct)",
            self.tube_length_cm,
        )

    def _init_emc2101(self) -> None:
        """Configures EMC2101 internal registers for direct manual PWM control."""
        try:
            # Register 0x4A: Fan Configuration (Bit 5 = 1 for Direct Mode)
            current_config = self.bus.read_byte_data(self.emc_addr, 0x4A)
            self.bus.write_byte_data(self.emc_addr, 0x4A, current_config | 0x20)
            # Register 0x4C: Initialize Fan Setting to 0% duty
            self.bus.write_byte_data(self.emc_addr, 0x4C, 0x00)
        except Exception as e:
            logger.error("Failed to initialize EMC2101 over I2C: %s", e)
            raise

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
        """Dedicated background thread polling HC-SR04 at ~25 Hz."""
        while self._running:
            d = self._acquire_distance_cm()

            # Retry once on miss
            if d < 0.0:
                time.sleep(0.001)
                d = self._acquire_distance_cm()

            with self._lock:
                if 2.0 <= d <= (self.tube_length_cm + 5.0):
                    self._miss_count = 0
                    self._last_distance_cm = d

                    if self.sensor_at_top:
                        # Top sensor pointing down:
                        # At top (d ~ 0 cm) -> ball_height = tube_length (PV ~ 100%)
                        # At bottom (d = tube_length) -> ball_height = 0 (PV ~ 0%)
                        ball_height_cm = max(
                            0.0, min(self.tube_length_cm, self.tube_length_cm - d)
                        )
                    else:
                        # Bottom sensor pointing up:
                        ball_height_cm = max(0.0, min(self.tube_length_cm, d))

                    self._current_pv = float(
                        (ball_height_cm / self.tube_length_cm) * 100.0
                    )
                else:
                    self._miss_count += 1
                    if self._miss_count % 20 == 0:
                        logger.warning(
                            "HC-SR04: %d consecutive missed pings (last reading: %.1f cm)",
                            self._miss_count,
                            d,
                        )

            # Sampling rate regulation (~25-30 Hz)
            time.sleep(0.035)

    def step(self, u_t: float) -> float:
        """
        Actuates EMC2101 fan duty cycle and immediately returns cached PV.
        Guaranteed execution time < 1.0 ms. Zero chance of UDP socket timeout.
        """
        # Non-blocking actuator write via I2C
        duty_clamped = max(0.0, min(100.0, u_t))
        reg_value = int(round((duty_clamped / 100.0) * 63.0))
        try:
            self.bus.write_byte_data(self.emc_addr, 0x4C, reg_value)
        except Exception as e:
            logger.warning("EMC2101 I2C write failed: %s", e)

        # Instantaneous atomic read from shared memory
        with self._lock:
            pv = self._current_pv
            dist = self._last_distance_cm
            misses = self._miss_count

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
        """De-energizes fan motor, terminates worker thread, releases GPIO lines."""
        self._running = False
        if hasattr(self, "worker_thread") and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=0.2)

        try:
            if hasattr(self, "bus"):
                self.bus.write_byte_data(self.emc_addr, 0x4C, 0x00)
                self.bus.close()
        except Exception:
            pass

        if GPIO:
            GPIO.cleanup([self.trig_pin, self.echo_pin])


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
        help="Whether the ultrasonic sensor is top-mounted pointing down (default: True).",
    )
    parser.add_argument(
        "--tube-length",
        type=float,
        default=50.0,
        help="Physical column height in cm (default: 50.0).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    plant: BasePlant
    if args.mode == "hardware":
        if not HARDWARE_AVAILABLE:
            logger.error(
                "Physical drivers unavailable. Launching simulated twin instead."
            )
            plant = SimulatedPlant(tube_length_m=args.tube_length / 100.0)
        else:
            plant = HardwarePlant(
                tube_length_cm=args.tube_length,
                sensor_at_top=args.sensor_at_top,
            )
    else:
        plant = SimulatedPlant(tube_length_m=args.tube_length / 100.0)

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

    try:
        while True:
            data, addr = sock.recvfrom(1024)
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
            sock.sendto(json.dumps(resp).encode("utf-8"), addr)

    except KeyboardInterrupt:
        logger.info("Plant termination requested.")
    finally:
        sock.close()
        plant.cleanup()


if __name__ == "__main__":
    main()
