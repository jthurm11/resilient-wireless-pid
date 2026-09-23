#!/usr/bin/env python3
"""
src/resilient_pid/controller/pid.py
===================================

Discrete PID Controller implementation with anti-windup clamping,
low-pass derivative filtering, and packet loss handling.

Project: Resilient Wireless Control: Hardening PID Loops against Network Jitter
"""

from typing import Tuple, Optional


class DiscretePID:
    """
    Discrete-time PID controller with anti-windup clamping,
    first-order low-pass derivative filtering, and packet loss hold support.
    """

    def __init__(
        self,
        kp: float = 0.35,
        ki: float = 0.12,
        kd: float = 0.06,
        dt: float = 0.05,
        output_limits: Tuple[float, float] = (0.0, 100.0),
        derivative_filter_tau: float = 0.02
    ):
        """
        Initialize the Discrete PID controller.

        Args:
            kp (float): Proportional gain.
            ki (float): Integral gain.
            kd (float): Derivative gain.
            dt (float): Sampling period in seconds (default: 0.05s / 50ms).
            output_limits (Tuple[float, float]): Saturation bounds (min, max).
            derivative_filter_tau (float): Time constant for derivative low-pass filter.
        """
        self.kp = kp
        self.ki = ki
        self.kd = kd
        self.dt = dt
        self.output_limits = output_limits
        self.tau = derivative_filter_tau

        # State registers
        self.integral = 0.0
        self.prev_error = 0.0
        self.filtered_derivative = 0.0

    def reset(self) -> None:
        """Reset internal state registers (integrator, error, derivative filter)."""
        self.integral = 0.0
        self.prev_error = 0.0
        self.filtered_derivative = 0.0

    def update(
        self,
        setpoint: float,
        pv: float,
        is_loss: bool = False
    ) -> float:
        """
        Calculate saturated discrete PID control output u[k].

        Args:
            setpoint (float): Reference target value SP[k].
            pv (float): Measured process variable feedback PV[k].
            is_loss (bool): Packet loss flag. If True, integral accumulation is suspended.

        Returns:
            float: Control effort u[k] bounded by output_limits.
        """
        error = setpoint - pv

        if not is_loss:
            # Trapezoidal / Euler integral accumulation
            self.integral += error * self.dt

            # Raw derivative calculation
            raw_derivative = (error - self.prev_error) / self.dt

            # Low-pass filter for derivative term to suppress noise amplification
            alpha = self.dt / (self.tau + self.dt)
            self.filtered_derivative += alpha * (raw_derivative - self.filtered_derivative)

            self.prev_error = error

        # Unclamped PID output
        u_unclamped = (
            (self.kp * error)
            + (self.ki * self.integral)
            + (self.kd * self.filtered_derivative)
        )

        # Anti-windup back-calculation / clamping
        u_min, u_max = self.output_limits
        u_clamped = max(u_min, min(u_max, u_unclamped))

        # Integrator clamping: prevent windup if output is saturated
        if u_unclamped != u_clamped and self.ki > 0:
            if (u_unclamped > u_max and error > 0) or (u_unclamped < u_min and error < 0):
                # Back-track integral step
                self.integral -= error * self.dt

        return u_clamped
