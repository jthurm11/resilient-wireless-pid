#!/usr/bin/env python3
"""
src/resilient_pid/controller/arx_model.py
Discrete ARX plant model and state estimator.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ARXModelParams:
    """Discrete 2nd-order ARX plant parameters calibrated at Ts = 50ms."""

    a1: float = -1.6705
    a2: float = 0.6967
    b1: float = 0.0142
    b2: float = 0.0121


class DiscreteARXModel:
    """Direct Form II recursive filter: A(z)y = B(z)u."""

    def __init__(self, params: ARXModelParams | None = None):
        self.params = params or ARXModelParams()
        self.reset()

    def reset(self) -> None:
        self.y_1 = 0.0
        self.y_2 = 0.0
        self.u_1 = 0.0
        self.u_2 = 0.0

    def step_autonomous(self, u_k: float) -> float:
        """Propagates model using internal state history (delay-free plant twin)."""
        y_next = (
            -self.params.a1 * self.y_1
            - self.params.a2 * self.y_2
            + self.params.b1 * self.u_1
            + self.params.b2 * self.u_2
        )
        self.y_2 = self.y_1
        self.y_1 = float(y_next)
        self.u_2 = self.u_1
        self.u_1 = u_k
        return self.y_1

    def step_observer(
        self, u_k: float, y_measured: float, is_loss: bool = False
    ) -> float:
        """Propagates model with sensor resynchronization on packet reception."""
        y_curr = self.y_1 if is_loss else y_measured
        y_next = (
            -self.params.a1 * y_curr
            - self.params.a2 * self.y_2
            + self.params.b1 * self.u_1
            + self.params.b2 * self.u_2
        )

        self.y_2 = y_curr
        self.y_1 = float(y_next) if is_loss else float(y_measured)
        self.u_2 = self.u_1
        self.u_1 = u_k
        return float(y_next)
