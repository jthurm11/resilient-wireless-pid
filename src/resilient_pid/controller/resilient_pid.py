"""
Resilient PID Controller utilizing a predictive linear observer
and zero-order hold (ZOH) state recovery during network dropouts.
"""
from typing import Tuple
from resilient_pid.controller.pid import DiscretePID

class ResilientPID:
    def __init__(self, kp: float, ki: float, kd: float, dt: float,
                 plant_gain: float = 8.0, plant_tau: float = 10.0,
                 output_limits: Tuple[float, float] = (-100.0, 100.0)):
        self.pid = DiscretePID(kp, ki, kd, dt, output_limits)
        self.dt = dt
        self.k = plant_gain
        self.tau = plant_tau
        self.y_est = 0.0
        self.last_u = 0.0

    def reset(self) -> None:
        self.pid.reset()
        self.y_est = 0.0
        self.last_u = 0.0

    def update(self, setpoint: float, pv_actual: float, is_loss: bool = False) -> float:
        if is_loss:
            dy = self.dt * (self.k * self.last_u - self.y_est) / self.tau
            self.y_est += dy
            feedback_pv = self.y_est
        else:
            self.y_est = pv_actual
            feedback_pv = pv_actual

        u_t = self.pid.update(setpoint, feedback_pv)
        self.last_u = u_t
        return u_t