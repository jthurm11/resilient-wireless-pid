from collections import deque
from resilient_pid.controller.pid import DiscretePID


class SmithPredictor:
    """
    Smith Predictor dead-time compensation using an ARX model
    and an O(1) collections.deque delay ring buffer
    """

    def __init__(
        self,
        kp: float,
        ki: float,
        kd: float,
        dt: float,
        plant_delay_ms: float = 100.0,
        output_limits: tuple[float, float] = (0.0, 100.0),
    ):
        self.pid = DiscretePID(kp, ki, kd, dt, output_limits)
        self.dt = dt
        self.output_limits = output_limits

        # Calibrated 2nd-order discrete model for PingPongPID: 
        #   G(z) = (b1*z^-1 + b2*z^-2)/(1 + a1*z^-1 + a2*z^-2)
        self.a1 = -1.6705
        self.a2 = 0.6967
        self.b1 = 0.0142
        self.b2 = 0.0121

        self.y_model_1 = 0.0
        self.y_model_2 = 0.0
        self.u_model_1 = 0.0
        self.u_model_2 = 0.0

        self.delay_steps = max(1, int(round(plant_delay_ms / (dt * 1000.0))))
        self.delay_buffer: deque[float] = deque([0.0] * self.delay_steps, maxlen=self.delay_steps)

    def reset(self) -> None:
        self.pid.reset()
        self.y_model_1 = 0.0
        self.y_model_2 = 0.0
        self.u_model_1 = 0.0
        self.u_model_2 = 0.0
        self.delay_buffer = deque([0.0] * self.delay_steps, maxlen=self.delay_steps)

    def update(self, setpoint: float, pv: float, is_loss: bool = False) -> float:
        y_model_delayed = self.delay_buffer[0]

        # Delay-free model prediction
        y_model_fast = (
            -self.a1 * self.y_model_1
            - self.a2 * self.y_model_2
            + self.b1 * self.u_model_1
            + self.b2 * self.u_model_2
        )

        smith_feedback = y_model_fast + (pv - y_model_delayed)
        u_t = self.pid.update(setpoint, smith_feedback)

        # Shift model states
        self.delay_buffer.append(y_model_fast)
        self.y_model_2 = self.y_model_1
        self.y_model_1 = y_model_fast
        self.u_model_2 = self.u_model_1
        self.u_model_1 = u_t

        return u_t