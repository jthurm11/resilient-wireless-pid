#!/usr/bin/env python3
"""
src/resilient_pid/plant/plant_interface.py
Unified Plant Runtime Daemon for Resilient Wireless PID DCS.
Provides an interchangeable interface between physical hardware (PWM/I2C)
and a realistic, continuous aerodynamic software twin (RK4 integration).
"""
import os
import sys
import time
import json
import socket
import logging
import argparse
from abc import ABC, abstractmethod

import numpy as np

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("PlantInterface")

try:
    import smbus2
    import RPi.GPIO as GPIO
    HARDWARE_AVAILABLE = True
except (ImportError, RuntimeError):
    smbus2 = None
    GPIO = None
    HARDWARE_AVAILABLE = False


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
        tube_length_m: float = 0.50,    # 50 cm physical acrylic tube
        ball_mass_kg: float = 0.0027,   # 2.7g standard ping pong ball
        ball_radius_m: float = 0.020,   # 40mm diameter
        g: float = 9.80665
    ):
        self.dt = dt
        self.L_tube = tube_length_m
        self.m = ball_mass_kg
        self.r = ball_radius_m
        self.g = g

        # Physical constants
        self.rho = 1.204        # Air density at 20°C (kg/m^3)
        self.cd = 0.47          # Sphere drag coefficient
        self.area = np.pi * (self.r ** 2)

        # Actuator curve: 9.5 m/s max airflow gives hover at ~48-52% PWM
        self.tau_fan = 0.22     # Rotational electromechanical time constant (s)
        self.v_air_max = 9.5    # Max steady-state airspeed (m/s)

        # State: [v_air (m/s), y_pos (m), v_ball (m/s)]
        self.state = np.array([0.0, 0.0, 0.0], dtype=np.float64)

        # Mechanical boundaries
        self.cor_bottom = 0.20
        self.cor_top = 0.25
        self.friction_coeff = 0.08

    def _dynamics(self, state: np.ndarray, u_clamped: float, turbulent_flow: float) -> np.ndarray:
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
        if y <= 0.0 and accel < 0.0:
            accel = 0.0
            v_ball = 0.0

        return np.array([d_v_air, v_ball, accel], dtype=np.float64)

    def step(self, u_t: float) -> float:
        u_clamped = float(np.clip(u_t, 0.0, 100.0))

        # Fluid turbulence: dynamic vortex shedding perturbation (proportional to fan speed)
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
    def __init__(self, pwm_pin: int = 18, pwm_freq: int = 25000, i2c_bus: int = 1, i2c_addr: int = 0x29):
        if not HARDWARE_AVAILABLE:
            raise RuntimeError("Hardware drivers missing. Run only on physical Raspberry Pi nodes.")
        self.pwm_pin = pwm_pin
        self.sensor_addr = i2c_addr

        GPIO.setmode(GPIO.BCM)
        GPIO.setup(self.pwm_pin, GPIO.OUT)
        self.pwm = GPIO.PWM(self.pwm_pin, pwm_freq)
        self.pwm.start(0.0)

        self.bus = smbus2.SMBus(i2c_bus)
        logger.info("Initialized PWM (Pin %d, %d Hz) and I2C (Bus %d, Addr 0x%02X)",
                    pwm_pin, pwm_freq, i2c_bus, i2c_addr)

    def step(self, u_t: float) -> float:
        duty = max(0.0, min(100.0, u_t))
        self.pwm.ChangeDutyCycle(duty)

        try:
            raw_bytes = self.bus.read_word_data(self.sensor_addr, 0x14)
            pv = float(raw_bytes * 0.1)
            return max(0.0, min(100.0, pv))
        except Exception as e:
            logger.warning("I2C read failed: %s", e)
            return 0.0

    def cleanup(self) -> None:
        if self.pwm:
            self.pwm.ChangeDutyCycle(0.0)
            self.pwm.stop()
        if GPIO:
            GPIO.cleanup()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Distributed Control System - Plant Node Daemon")
    parser.add_argument("--mode", choices=["hardware", "simulate"], default="hardware")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5005)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    plant: BasePlant
    if args.mode == "hardware":
        if not HARDWARE_AVAILABLE:
            logger.error("Physical drivers unavailable. Launching simulated twin instead.")
            plant = SimulatedPlant()
        else:
            plant = HardwarePlant()
    else:
        plant = SimulatedPlant()

    logger.info("Plant Service active | Mode: %s | Socket: %s:%d", args.mode.upper(), args.host, args.port)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
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

            resp = {
                "seq": seq,
                "pv": pv,
                "t_send": t_send,
                "t_echo": t_recv
            }
            sock.sendto(json.dumps(resp).encode("utf-8"), addr)

    except KeyboardInterrupt:
        logger.info("Plant termination requested.")
    finally:
        sock.close()
        plant.cleanup()


if __name__ == "__main__":
    main()