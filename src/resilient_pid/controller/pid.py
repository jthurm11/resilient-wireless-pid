#!/usr/bin/env python3
"""
src/resilient_pid/controller/pid.py
Hardened Discrete PID controller with hover feedforward, conditional anti-windup,
and low-pass derivative filtering on measurement.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PIDConfig:
    """Canonical loop gains, anti-windup saturation bounds, and filtering constants."""

    kp: float = 0.17
    ki: float = 0.020
    kd: float = 0.13
    dt: float = 0.05
    ff_hover: float = 38.5  # Empirical aerodynamic hover bias for SP ~ 50%
    output_limits: tuple[float, float] = (33.5, 58.0)  # [u_min_flight, u_max_flight]
    integral_limits: tuple[float, float] = (-12.0, 12.0)
    integral_band: float = 10.0  # Conditional integration band (|error| <= 10%)
    derivative_filter_tau: float = 0.30


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
            ff_hover (float): Feedforward hover bias for terminal velocity balance.
            integral_band (float): Maximum error magnitude allowed for active integration.
        """
        self.cfg = config or PIDConfig()
        self.kp: float = float(overrides.get("kp", self.cfg.kp))
        self.ki: float = float(overrides.get("ki", self.cfg.ki))
        self.kd: float = float(overrides.get("kd", self.cfg.kd))
        self.dt: float = float(overrides.get("dt", self.cfg.dt))
        self.ff_hover: float = float(overrides.get("ff_hover", self.cfg.ff_hover))
        self.output_limits: tuple[float, float] = overrides.get(
            "output_limits", self.cfg.output_limits
        )
        self.integral_limits: tuple[float, float] = overrides.get(
            "integral_limits", self.cfg.integral_limits
        )
        self.int_band: float = float(
            overrides.get("integral_band", self.cfg.integral_band)
        )
        self.tau: float = float(
            overrides.get("derivative_filter_tau", self.cfg.derivative_filter_tau)
        )

        # Baseline integral accumulator begins at zero; hover bias is carried by ff_hover
        self.integral = 0.0
        self.prev_pv: float | None = None
        self.filtered_derivative = 0.0

    def reset(self) -> None:
        """Reset internal integrator, previous PV, and derivative registers."""
        self.integral = 0.0
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
        # Complete actuator de-energization when shutdown is requested
        if setpoint <= 0.0:
            self.reset()
            return 0.0

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
            u_tentative = self.ff_hover + (self.kp * error) + self.integral - d_term

            # Conditional Integration & Clamping Anti-Windup:
            # 1. Gate accumulation to steady-state regulation zone and away from physical hardstops
            # 2. Only integrate if output is unconstrained or if action actively pulls output out of saturation
            u_min, u_max = self.output_limits
            if abs(error) <= self.int_band and 12.0 < pv < 88.0:
                if u_min < u_tentative < u_max:
                    self.integral += self.ki * error * self.dt
                elif u_tentative >= u_max and error < 0:
                    self.integral += self.ki * error * self.dt
                elif u_tentative <= u_min and error > 0:
                    self.integral += self.ki * error * self.dt
            else:
                # Decay accumulated error exponentially when outside the linear band
                self.integral *= 0.85

            # Bound integral state accumulator
            i_min, i_max = self.integral_limits
            self.integral = max(i_min, min(i_max, self.integral))
        else:
            d_term = 0.0

        u_unclamped = (
            self.ff_hover
            + (self.kp * error)
            + self.integral
            - (self.kd * self.filtered_derivative)
        )
        u_min, u_max = self.output_limits

        # If commanding a resting setpoint, bypass active flight floor clamp
        effective_u_min = 0.0 if setpoint <= 5.0 else u_min
        return float(max(effective_u_min, min(u_max, u_unclamped)))
