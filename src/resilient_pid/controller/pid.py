#!/usr/bin/env python3
"""
src/resilient_pid/controller/pid.py
Discrete PID controller with anti-windup clamping and derivative filtering.
Provides primary (discrete) actuation and integral hold logic for loss events.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PIDConfig:
    """Canonical loop gains, anti-windup saturation bounds, and filtering constants."""

    kp: float = 0.16
    ki: float = 0.025
    kd: float = 0.12
    dt: float = 0.05
    output_limits: tuple[float, float] = (0.0, 78.0)
    integral_limits: tuple[float, float] = (0.0, 70.0)
    derivative_filter_tau: float = 0.35


class DiscretePID:
    """
    Discrete-time PID controller.
    Supports anti-windup clamping, low-pass derivative filtering, and freeze-on-loss.
    """

    def __init__(self, config: PIDConfig | None = None, **overrides):
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

        self.cfg = config or PIDConfig()
        self.kp: float = float(overrides.get("kp", self.cfg.kp))
        self.ki: float = float(overrides.get("ki", self.cfg.ki))
        self.kd: float = float(overrides.get("kd", self.cfg.kd))
        self.dt: float = float(overrides.get("dt", self.cfg.dt))
        self.output_limits: tuple[float, float] = overrides.get(
            "output_limits", self.cfg.output_limits
        )
        self.integral_limits: tuple[float, float] = overrides.get(
            "integral_limits", self.cfg.integral_limits
        )
        self.tau: float = float(
            overrides.get("derivative_filter_tau", self.cfg.derivative_filter_tau)
        )
        # Pre-charge integrator near baseline float equilibrium
        self.integral = self.integral_limits[0]
        self.prev_pv: float | None = None
        self.filtered_derivative = 0.0

    def reset(self) -> None:
        """Reset internal integrator, previous PV, and derivative registers."""
        self.integral = self.integral_limits[0]
        self.prev_pv = None
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
            # Low-pass filtered derivative on measurement (dPV/dt)
            if self.prev_pv is None:
                self.prev_pv = pv

            delta_pv = pv - self.prev_pv
            raw_d_pv = delta_pv / self.dt
            alpha = self.dt / (self.tau + self.dt)
            self.filtered_derivative += alpha * (raw_d_pv - self.filtered_derivative)
            self.prev_pv = pv

            # Evaluate tentative unconstrained output
            d_term = self.kd * self.filtered_derivative
            u_tentative = (self.kp * error) + self.integral - d_term

            # Clamping Anti-Windup: only integrate if output is not saturated,
            # or if integration acts to pull the output back inside limits
            u_min, u_max = self.output_limits
            if u_min < u_tentative < u_max:
                self.integral += self.ki * error * self.dt
            elif u_tentative >= u_max and error < 0:
                self.integral += self.ki * error * self.dt
            elif u_tentative <= u_min and error > 0:
                self.integral += self.ki * error * self.dt

            # Bound integral state accumulator
            i_min, i_max = self.integral_limits
            self.integral = max(i_min, min(i_max, self.integral))
        else:
            d_term = 0.0

        u_unclamped = (self.kp * error) + self.integral - (self.kd * self.filtered_derivative)
        u_min, u_max = self.output_limits
        return float(max(u_min, min(u_max, u_unclamped)))
