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
        kp: float = 0.45,
        ki: float = 0.12,
        kd: float = 0.10,
        dt: float = 0.05,
        output_limits: tuple[float, float] = (0.0, 100.0),
        integral_limits: tuple[float, float] = (35.0, 75.0),
        derivative_filter_tau: float = 0.15,
    ):
        """
        Initialize discrete PID gains and operational bounds.

        Args:
            kp (float): Proportional gain.
            ki (float): Integral gain.
            kd (float): Derivative gain.
            dt (float): Sampling interval in seconds.
            output_limits (Tuple[float, float]): Output saturation bounds (min, max).
            integral_limits (Tuple[float, float]): Internal integrator saturation bounds (min, max).
            derivative_filter_tau (float): Filter time constant for derivative term.
        """
        self.kp = kp
        self.ki = ki
        self.kd = kd
        self.dt = dt
        self.output_limits = output_limits
        self.integral_limits = integral_limits
        self.tau = derivative_filter_tau

        # Pre-charge integrator to baseline levitation equilibrium
        self.integral = 45.0
        self.prev_pv: float | None = None
        self.filtered_derivative = 0.0

    def reset(self) -> None:
        """Reset internal integrator, previous PV, and derivative registers."""
        self.integral = 0.0
        self.prev_pv = 0.0
        self.filtered_derivative = 0.0

    def update(self, setpoint: float, pv: float, is_loss: bool = False) -> float:
        """
        Calculate saturated discrete PID control output u[k].
        Derivative on Measurement (PV) to eliminate setpoint derivative kick.

        Args:
            setpoint (float): Target reference value SP[k].
            pv (float): Measured process variable feedback PV[k].
            is_loss (bool): Packet loss indicator; suspends integration when True.

        Returns:
            float: Saturated control effort u[k] bounded by output_limits.
        """
        error = setpoint - pv

        if not is_loss:
            # Forward Euler integration with bounded clamp
            self.integral += self.ki * error * self.dt
            i_min, i_max = self.integral_limits
            self.integral = max(i_min, min(i_max, self.integral))

            # Derivative on Measurement (dPV/dt) with low-pass filtering
            if self.prev_pv is None:
                self.prev_pv = pv

            delta_pv = pv - self.prev_pv
            raw_d_pv = delta_pv / self.dt
            alpha = self.dt / (self.tau + self.dt)
            self.filtered_derivative += alpha * (raw_d_pv - self.filtered_derivative)
            self.prev_pv = pv

        # Parallel form: u = Kp*e + I - Kd*(dPV/dt)
        # Note: Derivative acts with negative feedback against positive PV growth
        u_unclamped = (
            (self.kp * error) + self.integral - (self.kd * self.filtered_derivative)
        )

        # Output saturation
        u_min, u_max = self.output_limits
        u_clamped = max(u_min, min(u_max, u_unclamped))

        return u_clamped
