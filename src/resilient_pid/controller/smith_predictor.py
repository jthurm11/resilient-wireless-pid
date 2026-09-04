from typing import Tuple
from resilient_pid.controller.pid import DiscretePID

class SmithPredictor:
    """
    Smith Predictor dead-time compensation using an internal linear
    model and circular delay buffer to cancel transport lag.
    """
    def __init__(self, kp: float, ki: float, kd: float, dt: float,
                 plant_gain: float = 8.0, plant_tau: float = 10.0, plant_delay_ms: float = 100.0,
                 output_limits: Tuple[float, float] = (-100.0, 100.0)):
        self.pid = DiscretePID(kp, ki, kd, dt, output_limits)
        self.dt = dt
        self.k = plant_gain
        self.tau = plant_tau

        self.delay_steps = max(1, int(plant_delay_ms / (dt * 1000.0)))
        self.model_y_buffer = [0.0] * self.delay_steps
        self.y_model_undelayed = 0.0

    def reset(self) -> None:
        self.pid.reset()
        self.model_y_buffer = [0.0] * self.delay_steps
        self.y_model_undelayed = 0.0

    def update(self, setpoint: float, pv: float) -> float:
        y_model_delayed = self.model_y_buffer[0]
        smith_feedback = self.y_model_undelayed + (pv - y_model_delayed)

        u_t = self.pid.update(setpoint, smith_feedback)

        dy = self.dt * (self.k * u_t - self.y_model_undelayed) / self.tau
        self.y_model_undelayed += dy

        self.model_y_buffer.pop(0)
        self.model_y_buffer.append(self.y_model_undelayed)
        return u_t