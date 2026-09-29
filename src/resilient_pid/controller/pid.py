#!/usr/bin/env python3
"""
pid.py
Discrete PID controller with anti-windup clamping and derivative filtering.
Provides baseline actuation and integral hold logic for loss events.
"""


class DiscretePID:
    """
    Discrete-time PID controller.
    Supports anti-windup clamping, low-pass derivative filtering, and freeze-on-loss.
    """

    def __init__(
        self,
        kp: float = 0.35,
        ki: float = 0.12,
        kd: float = 0.06,
        dt: float = 0.05,
        output_limits: tuple[float, float] = (0.0, 100.0),
        derivative_filter_tau: float = 0.02,
    ):
        """
        Initialize discrete PID gains and operational bounds.

        Args:
            kp (float): Proportional gain.
            ki (float): Integral gain.
            kd (float): Derivative gain.
            dt (float): Sampling interval in seconds.
            output_limits (Tuple[float, float]): Output saturation bounds (min, max).
            derivative_filter_tau (float): Filter time constant for derivative term.
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
        """Reset internal integrator, previous error, and derivative registers."""
        self.integral = 0.0
        self.prev_error = 0.0
        self.filtered_derivative = 0.0

    def update(self, setpoint: float, pv: float, is_loss: bool = False) -> float:
        """
        Calculate saturated discrete PID control output u[k].

        Args:
            setpoint (float): Target reference value SP[k].
            pv (float): Measured process variable feedback PV[k].
            is_loss (bool): Packet loss indicator; suspends integration when True.

        Returns:
            float: Saturated control effort u[k] bounded by output_limits.
        """
        error = setpoint - pv

        if not is_loss:
            # Forward Euler integral accumulation
            self.integral += error * self.dt

            # First-order low-pass filtered derivative update
            raw_derivative = (error - self.prev_error) / self.dt
            alpha = self.dt / (self.tau + self.dt)
            self.filtered_derivative += alpha * (
                raw_derivative - self.filtered_derivative
            )

            self.prev_error = error

        # Unclamped control effort summation
        u_unclamped = (
            (self.kp * error)
            + (self.ki * self.integral)
            + (self.kd * self.filtered_derivative)
        )

        # Output saturation enforcement
        u_min, u_max = self.output_limits
        u_clamped = max(u_min, min(u_max, u_unclamped))

        # Anti-windup back-calculation to clamp runaway integration
        if u_unclamped != u_clamped and self.ki > 0:
            if (u_unclamped > u_max and error > 0) or (
                u_unclamped < u_min and error < 0
            ):
                self.integral -= error * self.dt

        return u_clamped
