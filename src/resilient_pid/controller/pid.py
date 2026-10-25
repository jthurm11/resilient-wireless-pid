#!/usr/bin/env python3
"""
src/resilient_pid/controller/pid.py
Canonical Discrete PID controller with hover feedforward, accelerated takeoff ramp,
calibrated acoustic noise filtering, and smooth high-elevation release.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PIDConfig:
    """Canonical loop gains, anti-windup bounds, and feedforward constants."""

    kp: float = 0.16  # Proportional stiffness
    ki: float = 0.022  # Steady-state error elimination
    kd: float = 0.15  # Damping gain
    dt: float = 0.05
    ff_hover: float = 33.5  # Calibrated empirical float equilibrium for SP ~ 50%
    ki_takeoff: float = 0.14  # Accelerated takeoff gain (liftoff in ~2.5s)
    output_limits: tuple[float, float] = (20.0, 75.0)  # In-flight operational envelope
    integral_limits: tuple[float, float] = (-12.0, 30.0)
    derivative_filter_tau: float = 0.35  # Suppresses ultrasonic echo jitter


class DiscretePID:
    """
    Discrete-time PID controller with continuous aerodynamic damping.
    """

    def __init__(self, config: PIDConfig | None = None, **overrides):
        self.cfg = config or PIDConfig()
        self.kp: float = float(overrides.get("kp", self.cfg.kp))
        self.ki: float = float(overrides.get("ki", self.cfg.ki))
        self.kd: float = float(overrides.get("kd", self.cfg.kd))
        self.dt: float = float(overrides.get("dt", self.cfg.dt))
        self.ff_hover: float = float(overrides.get("ff_hover", self.cfg.ff_hover))
        self.ki_takeoff: float = float(overrides.get("ki_takeoff", self.cfg.ki_takeoff))
        self.output_limits: tuple[float, float] = overrides.get(
            "output_limits", self.cfg.output_limits
        )
        self.integral_limits: tuple[float, float] = overrides.get(
            "integral_limits", self.cfg.integral_limits
        )
        self.tau: float = float(
            overrides.get("derivative_filter_tau", self.cfg.derivative_filter_tau)
        )

        self.integral = 0.0
        self.prev_pv: float | None = None
        self.filtered_derivative = 0.0
        self._liftoff_armed = False
        self._floor_confirm_count = 0

    def reset(self) -> None:
        """Reset internal integrator, registers, and state flags."""
        self.integral = 0.0
        self.prev_pv = None
        self.filtered_derivative = 0.0
        self._liftoff_armed = False
        self._floor_confirm_count = 0

    def update(self, setpoint: float, pv: float, is_loss: bool = False) -> float:
        """Calculate saturated discrete PID control output u[k]."""
        if setpoint <= 0.0:
            self.reset()
            return 0.0

        error = setpoint - pv

        if not is_loss:
            # Floor confirmation debounce (requires 3 consecutive samples < 12%)
            if pv < 12.0:
                self._floor_confirm_count += 1
                if self._floor_confirm_count >= 3:
                    self._liftoff_armed = True
            else:
                self._floor_confirm_count = 0

            # Takeoff dislodge hand-off to hover baseline
            just_dislodged = False
            if self._liftoff_armed and pv >= 16.0:
                self.integral = 0.0
                self._liftoff_armed = False
                just_dislodged = True

            # Derivative on measurement with physical velocity slew-clamp
            #   Max ballistic speed in duct ~ 2.5 m/s (~550% PV/sec -> max |delta_pv| ~ 27.5% per 50ms)
            if self.prev_pv is None:
                self.prev_pv = pv

            delta_pv = pv - self.prev_pv
            delta_pv_clamped = max(-25.0, min(25.0, delta_pv))
            raw_d_pv = delta_pv_clamped / self.dt
            alpha = self.dt / (self.tau + self.dt)
            self.filtered_derivative += alpha * (raw_d_pv - self.filtered_derivative)
            self.prev_pv = pv

            d_term = self.kd * self.filtered_derivative
            u_tentative = self.ff_hover + (self.kp * error) + self.integral - d_term

            # Integral anti-windup clamping
            if not just_dislodged:
                active_ki = (
                    self.ki_takeoff if (self._liftoff_armed and pv < 14.0) else self.ki
                )
                u_min, u_max = self.output_limits
                if u_min < u_tentative < u_max:
                    self.integral += active_ki * error * self.dt
                elif u_tentative >= u_max and error < 0:
                    self.integral += active_ki * error * self.dt
                elif u_tentative <= u_min and error > 0:
                    self.integral += active_ki * error * self.dt

            i_min, i_max = self.integral_limits
            self.integral = max(i_min, min(i_max, self.integral))
        else:
            # Under packet loss, freeze derivative contribution
            d_term = 0.0

        u_unclamped = self.ff_hover + (self.kp * error) + self.integral - d_term

        # High-elevation release: drop minimum floor down to 18.0% to break Bernoulli suction
        if pv >= 80.0:
            effective_u_min = 18.0
            self.integral = min(0.0, self.integral)
        else:
            effective_u_min = self.output_limits[0]

        return float(max(effective_u_min, min(self.output_limits[1], u_unclamped)))
