#!/usr/bin/env python3
"""
src/resilient_pid/plant/plant_interface.py
Unified Plant Runtime Daemon for Resilient Wireless PID DCS.
Provides an interchangeable interface between physical hardware (PWM/I2C)
and a continuous second-order software twin (LXC/x86).
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

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("PlantInterface")

# Conditional import of physical hardware drivers
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
        Accepts the commanded control effort u(t) and advances the plant dynamics.
        
        :param u_t: Control input (normalized percentage or engineering units)
        :return: Current process variable feedback pv(t)
        """
        pass

    @abstractmethod
    def cleanup(self) -> None:
        """Safely de-energize physical actuators or release socket/driver handles."""
        pass


class SimulatedPlant(BasePlant):
    """
    Second-order aerodynamic software twin modeling the ball-and-tube testbed.
    Integrates mechanical drag, lift coefficients, and gravitational acceleration,
    with zero external hardware peripheral dependencies.
    """
    def __init__(self, dt: float = 0.05, mass: float = 0.0027, g: float = 9.81):
        self.dt = dt
        self.m = mass
        self.g = g
        self.y = 0.0          # Process Variable: Height (0.0 to 100.0%)
        self.v = 0.0          # Velocity (m/s)
        self.k_fan = 0.25      # Aerodynamic lift scaling
        self.k_drag = 0.05     # Air resistance coefficient

    def step(self, u_t: float) -> float:
        # Enforce actuator boundary limits
        u_clamped = max(0.0, min(100.0, u_t))

        # Equations of motion: m * a = F_fan - F_drag - F_g
        f_fan = self.k_fan * u_clamped
        f_drag = self.k_drag * self.v * abs(self.v)
        accel = (f_fan - f_drag - (self.m * self.g)) / self.m

        # Forward Euler numerical integration
        self.v += accel * self.dt
        self.y += self.v * self.dt

        # Mechanical tube hard-stops (restitution damping)
        if self.y <= 0.0:
            self.y = 0.0
            self.v = 0.0
        elif self.y >= 100.0:
            self.y = 100.0
            self.v = 0.0

        # Inject white Gaussian transducer noise: N(0, sigma^2)
        sensor_noise = np.random.normal(0.0, 0.15)
        return float(np.clip(self.y + sensor_noise, 0.0, 100.0))

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
        
        # Setup GPIO and Hardware PWM
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(self.pwm_pin, GPIO.OUT)
        self.pwm = GPIO.PWM(self.pwm_pin, pwm_freq)
        self.pwm.start(0.0)

        # Setup I2C Bus Driver
        self.bus = smbus2.SMBus(i2c_bus)
        logger.info("Initialized physical PWM (Pin %d, %d Hz) and I2C (Bus %d, Addr 0x%02X)",
                    pwm_pin, pwm_freq, i2c_bus, i2c_addr)

    def step(self, u_t: float) -> float:
        # Actuate PWM duty cycle
        duty = max(0.0, min(100.0, u_t))
        self.pwm.ChangeDutyCycle(duty)

        # Transducer read via I2C word registers
        try:
            raw_bytes = self.bus.read_word_data(self.sensor_addr, 0x14)
            # Calibration transform from sensor millivolts/ticks to tube height percentage
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

    # Instantiate the selected plant engine
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
            # Block waiting for control effort datagram from ctrl-node-01
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

            # Advance plant dynamics
            pv = plant.step(u_t)

            # Transmit process variable feedback response
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