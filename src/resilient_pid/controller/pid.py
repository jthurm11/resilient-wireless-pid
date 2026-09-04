"""
Discrete PID Controller with trapezoidal integration,
derivative low-pass filtering, and anti-windup clamping.
"""
from typing import Tuple

class DiscretePID:
    def __init__(self, kp: float, ki: float, kd: float, dt: float,
                 output_limits: Tuple[float, float] = (-100.0, 100.0),
                 derivative_filter_tau: float = 0.02):
        self.kp = kp
        self.ki = ki
        self.kd = kd
        self.dt = dt
        self.output_limits = output_limits
        self.filter_tau = derivative_filter_tau

        self.integral = 0.0
        self.last_error = 0.0
        self.last_derivative = 0.0
        self.last_pv = 0.0

    def reset(self) -> None:
        self.integral = 0.0
        self.last_error = 0.0
        self.last_derivative = 0.0
        self.last_pv = 0.0

    def update(self, setpoint: float, pv: float) -> float:
        error = setpoint - pv
        self.integral += 0.5 * (error + self.last_error) * self.dt

        raw_derivative = (error - self.last_error) / self.dt if self.dt > 0 else 0.0
        alpha = self.dt / (self.filter_tau + self.dt)
        derivative = alpha * raw_derivative + (1.0 - alpha) * self.last_derivative
        self.last_derivative = derivative

        u_t = (self.kp * error) + (self.ki * self.integral) + (self.kd * derivative)
        lower_lim, upper_lim = self.output_limits

        if u_t > upper_lim:
            u_t_saturated = upper_lim
            if self.ki != 0.0:
                self.integral -= (u_t - upper_lim) / self.ki
        elif u_t < lower_lim:
            u_t_saturated = lower_lim
            if self.ki != 0.0:
                self.integral += (lower_lim - u_t) / self.ki
        else:
            u_t_saturated = u_t

        self.last_error = error
        self.last_pv = pv
        return u_t_saturated