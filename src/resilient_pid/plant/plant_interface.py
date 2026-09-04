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
from typing import Tuple

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
        ball_mass_kg: float = 0.0027,    # 2.7g standard ping pong ball
        ball_radius_m: float = 0.020,   # 40mm diameter
        g: float = 9.80665
    ):
        self.dt = dt
        self.L_tube = tube_length_m
        self.m = ball_mass_kg
        self.r = ball_radius_m
        self.g = g
        
        # Aerodynamic constants (Air at 20°C: rho = 1.204 kg/m^3, Sphere Cd = 0.47)
        self.rho = 1.204
        self.cd = 0.47
        self.area = np.pi * (self.r ** 2)
        
        # Fan dynamics: First-order motor response
        # tau_fan: time to 63.2% max blower speed (~180ms electromechanical inertia)
        self.tau_fan = 0.18
        self.v_air_max = 8.8  # Max steady-state airflow velocity at 100% PWM (m/s)
        
        # State vector: [v_air (m/s), y_pos (m), v_ball (m/s)]
        self.state = np.array([0.0, 0.0, 0.0], dtype=np.float64)
        
        # Mechanical boundaries & wall damping
        self.cor_bottom = 0.15  # Coefficient of restitution at wire mesh floor
        self.cor_top = 0.20     # Coefficient of restitution at top mesh restrictor
        self.friction_coeff = 0.05  # Sliding/viscous friction along acrylic wall

    def _dynamics(self, state: np.ndarray, u_clamped: float) -> np.ndarray:
        """
        Continuous non-linear system state-space derivatives dx/dt = f(x, u).
        x = [v_air, y, v_ball]
        """
        v_air, y, v_ball = state
        
        # 1. Blower electromechanical lag
        target_v_air = (u_clamped / 100.0) * self.v_air_max
        d_v_air = (target_v_air - v_air) / self.tau_fan
        
        # 2. Pressure gradient drop near top exhaust (open tube end effect)
        # Air velocity slightly diminishes toward the open exit
        exhaust_loss = 1.0 - 0.15 * (y / self.L_tube)
        effective_v_air = max(0.0, v_air * exhaust_loss)
        
        # 3. Aerodynamic drag computed from relative fluid velocity
        v_rel = effective_v_air - v_ball
        f_aero = 0.5 * self.rho * self.cd * self.area * v_rel * abs(v_rel)
        
        # 4. Net linear acceleration: a = (F_aero - F_gravity - F_wall) / m
        wall_friction = self.friction_coeff * v_ball
        accel = (f_aero / self.m) - self.g - wall_friction
        
        # Prevent fictitious ground penetration when resting at mesh bottom
        if y <= 0.0 and accel < 0.0:
            accel = 0.0
            v_ball = 0.0
            
        return np.array([d_v_air, v_ball, accel], dtype=np.float64)

    def step(self, u_t: float) -> float:
        # Actuator physical saturation
        u_clamped = float(np.clip(u_t, 0.0, 100.0))
        
        # 4th-Order Runge-Kutta numerical integration across dt
        k1 = self._dynamics(self.state, u_clamped)
        k2 = self._dynamics(self.state + 0.5 * self.dt * k1, u_clamped)
        k3 = self._dynamics(self.state + 0.5 * self.dt * k2, u_clamped)
        k4 = self._dynamics(self.state + self.dt * k3, u_clamped)
        
        self.state += (self.dt / 6.0) * (k1 + 2.0 * k2 + 2.0 * k3 + k4)
        
        # Enforce kinematic constraints (hard boundary collisions)
        # Bottom floor mesh
        if self.state[1] <= 0.0:
            self.state[1] = 0.0
            if self.state[2] < 0.0:
                self.state[2] = -self.state[2] * self.cor_bottom
                if abs(self.state[2]) < 0.02:
                    self.state[2] = 0.0
                    
        # Top restrictor mesh
        elif self.state[1] >= self.L_tube:
            self.state[1] = self.L_tube
            if self.state[2] > 0.0:
                self.state[2] = -self.state[2] * self.cor_top
                if abs(self.state[2]) < 0.02:
                    self.state[2] = 0.0

        # Map position to percentage: y_pct = (y_meters / L_tube) * 100.0
        pv_true = (self.state[1] / self.L_tube) * 100.0
        
        # Realistic Time-of-Flight (ToF) / Ultrasonic sensor noise:
        # - Gaussian white noise (sigma = 0.12%)
        # - Occasional quantization jitter / specular reflection noise
        sensor_noise = np.random.normal(0.0, 0.12)
        pv_measured = float(np.clip(pv_true + sensor_noise, 0.0, 100.0))
        
        return pv_measured

    def cleanup(self) -> None:
        logger.info("De-allocating simulated plant numerical state.")


class HardwarePlant(BasePlant):
    """
    Physical hardware interface communicating across Linux sysfs PWM 
    and I2C hardware buses on bare-metal Raspberry Pi nodes.
    """
    def __init__(self, pwm_pin: int = 18, pwm_freq: int = 25000, i2c_bus: int = 1, i2c_addr: int = 0x29):
        if not HARDWARE_AVAILABLE:
            raise RuntimeError(
                "Hardware drivers (RPi.GPIO/smbus2) unavailable. "
                "Ensure this node is a bare-metal Raspberry Pi or pass '--mode simulate'."
            )
        self.pwm_pin = pwm_pin
        self.sensor_addr = i2c_addr
        
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(self.pwm_pin, GPIO.OUT)
        self.pwm = GPIO.PWM(self.pwm_pin, pwm_freq)
        self.pwm.start(0.0)

        self.bus = smbus2.SMBus(i2c_bus)
        logger.info("Initialized physical PWM (Pin %d, %d Hz) and I2C (Bus %d, Addr 0x%02X)",
                    pwm_pin, pwm_freq, i2c_bus, i2c_addr)

    def step(self, u_t: float) -> float:
        duty = max(0.0, min(100.0, u_t))
        self.pwm.ChangeDutyCycle(duty)

        try:
            raw_bytes = self.bus.read_word_data(self.sensor_addr, 0x14)
            pv = float(raw_bytes * 0.1)
            return max(0.0, min(100.0, pv))
        except Exception as e:
            logger.warning("I2C Bus read failed: %s", e)
            return 0.0

    def cleanup(self) -> None:
        logger.info("De-energizing physical actuators and clearing GPIO...")
        if self.pwm:
            self.pwm.ChangeDutyCycle(0.0)
            self.pwm.stop()
        if GPIO:
            GPIO.cleanup()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Distributed Control System - Plant Node Daemon")
    parser.add_argument(
        "--mode", 
        choices=["hardware", "simulate"], 
        default="hardware",
        help="Execution target: 'hardware' (physical Pi GPIO/I2C) or 'simulate' (LXC/x86 numerical twin)"
    )
    parser.add_argument("--host", default="0.0.0.0", help="Socket binding address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=5005, help="UDP ingress port (default: 5005)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    plant: BasePlant
    if args.mode == "hardware":
        if not HARDWARE_AVAILABLE:
            logger.error("Physical drivers not installed or non-ARM host. Refusing to launch in hardware mode.")
            sys.exit(1)
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
                logger.warning("Corrupted datagram received from %s", addr)
                continue

            seq = pkt.get("seq")
            u_t = float(pkt.get("u", 0.0))
            t_send = pkt.get("t_send")

            # Advance non-linear aerodynamic ODEs
            pv = plant.step(u_t)

            # Transmit echo feedback
            resp = {
                "seq": seq,
                "pv": pv,
                "t_send": t_send,
                "t_echo": t_recv
            }
            sock.sendto(json.dumps(resp).encode("utf-8"), addr)

    except KeyboardInterrupt:
        logger.info("Plant termination requested by operator.")
    finally:
        sock.close()
        plant.cleanup()
        logger.info("Plant Daemon shutdown cleanly.")


if __name__ == "__main__":
    main()