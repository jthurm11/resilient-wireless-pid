#!/usr/bin/env python3
"""
src/resilient_pid/controller/resilient_pid.py
Resilient predictive observer PID controller for mitigating packet dropouts.
"""

try:
    from resilient_pid.controller.arx_model import ARXModelParams, DiscreteARXModel
    from resilient_pid.controller.pid import DiscretePID, PIDConfig
except ImportError:
    from .arx_model import ARXModelParams, DiscreteARXModel
    from .pid import DiscretePID, PIDConfig


class ResilientPID:
    """Predictive observer PID controller that switches to state estimation during loss events."""

    def __init__(
        self,
        config: PIDConfig | None = None,
        model_params: ARXModelParams | None = None,
    ):
        self.cfg = config or PIDConfig()
        self.pid = DiscretePID(config=self.cfg)
        self.model = DiscreteARXModel(params=model_params)
        self.y_est = 0.0
        self.consecutive_losses = 0

    def reset(self) -> None:
        self.pid.reset()
        self.model.reset()
        self.y_est = 0.0
        self.consecutive_losses = 0

    def update(
        self,
        setpoint: float,
        pv: float = 0.0,
        is_loss: bool = False,
        pv_actual: float | None = None,
    ) -> float:
        measured_pv = pv_actual if pv_actual is not None else pv

        if not is_loss:
            self.consecutive_losses = 0
            u_t = self.pid.update(setpoint=setpoint, pv=measured_pv, is_loss=False)
            self.y_est = self.model.step_observer(
                u_k=u_t, y_measured=measured_pv, is_loss=False
            )
        else:
            self.consecutive_losses += 1
            # Execute primary control on predicted process variable and freeze integrator
            u_t = self.pid.update(setpoint=setpoint, pv=self.y_est, is_loss=True)
            self.y_est = self.model.step_observer(
                u_k=u_t, y_measured=self.y_est, is_loss=True
            )

        return u_t
