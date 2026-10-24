#!/usr/bin/env python3
"""
src/resilient_pid/controller/smith_predictor.py
Discrete Smith Predictor engine for transport dead-time compensation.
Decouples network latency from the closed-loop characteristic equation.
"""

from collections import deque

try:
    from resilient_pid.controller.arx_model import ARXModelParams, DiscreteARXModel
    from resilient_pid.controller.pid import DiscretePID, PIDConfig
except ImportError:
    from .arx_model import ARXModelParams, DiscreteARXModel
    from .pid import DiscretePID, PIDConfig


class SmithPredictor:
    """
    Discrete Smith Predictor controller.
    Pairs a discrete ARX plant model with an O(1) circular ring buffer.
    """

    def __init__(
        self,
        config: PIDConfig | None = None,
        model_params: ARXModelParams | None = None,
        plant_delay_ms: float = 0.0,
    ):
        self.cfg = config or PIDConfig()
        self.pid = DiscretePID(config=self.cfg)
        self.model = DiscreteARXModel(params=model_params)
        self.plant_delay_ms = plant_delay_ms

        self.delay_steps = int(round(self.plant_delay_ms / (self.cfg.dt * 1000.0)))
        buffer_len = max(1, self.delay_steps)
        self.delay_buffer: deque[float] = deque([0.0] * buffer_len, maxlen=buffer_len)

    def set_dead_time(self, plant_delay_ms: float) -> None:
        self.plant_delay_ms = plant_delay_ms
        self.delay_steps = int(round(self.plant_delay_ms / (self.cfg.dt * 1000.0)))
        buffer_len = max(1, self.delay_steps)
        self.delay_buffer = deque([0.0] * buffer_len, maxlen=buffer_len)

    def reset(self) -> None:
        self.pid.reset()
        self.model.reset()
        buffer_len = max(1, self.delay_steps)
        self.delay_buffer = deque([0.0] * buffer_len, maxlen=buffer_len)

    def update(self, setpoint: float, pv: float, is_loss: bool = False) -> float:
        # If latency is negligible (< 25 ms), collapse directly to primary PID
        if self.delay_steps == 0:
            return self.pid.update(setpoint=setpoint, pv=pv, is_loss=is_loss)

        # Retrieve delayed model feedback
        y_model_delayed = self.delay_buffer[0] if len(self.delay_buffer) > 0 else 0.0

        # Step fast delay-free internal twin
        y_model_fast = self.model.step_autonomous(u_k=self.model.u_1)

        # Calculate reconstructed Smith feedback: y_fast + (pv - y_delayed)
        smith_feedback = y_model_fast + (pv - y_model_delayed)

        # Primary control action calculation
        u_t = self.pid.update(setpoint=setpoint, pv=smith_feedback, is_loss=is_loss)

        # Enqueue fast estimate and update actuator memory
        self.delay_buffer.append(y_model_fast)
        self.model.u_1 = u_t
        return u_t
