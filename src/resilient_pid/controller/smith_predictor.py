#!/usr/bin/env python3
"""
src/resilient_pid/controller/smith_predictor.py
================================================

Discrete Smith Predictor implementation for dead-time compensation in 
networked control systems.

Project: Resilient Wireless Control: Hardening PID Loops against Network Jitter
Task 4.1: Smith Predictor Dead-Time Compensation Engine

Mathematical Formulation:
-------------------------
The Smith Predictor decouples transport latency (theta) from the closed-loop
characteristic equation by comparing the delayed physical feedback variable (pv)
against an internal discrete model prediction.

1. Calibrated 2nd-Order ARX Discrete Plant Model (Ts = 0.05 s):
   G_p0(z) = (b1*z^-1 + b2*z^-2) / (1 + a1*z^-1 + a2*z^-2)
   y_model_fast[k] = -a1*y_model[k-1] - a2*y_model[k-2] + b1*u[k-1] + b2*u[k-2]
   (a1 = -1.6705, a2 = 0.6967, b1 = 0.0142, b2 = 0.0121)

2. Circular Delay Ring Buffer (O(1) collections.deque):
   d_steps = max(1, int(round(plant_delay_ms / (dt * 1000.0))))
   y_model_delayed[k] = delay_buffer[0]

3. Smith Feedback & Error Calculation:
   smith_feedback[k] = y_model_fast[k] + (pv_actual[k] - y_model_delayed[k])
   u[k] = PID.update(setpoint[k], smith_feedback[k])
"""

from collections import deque
from typing import Tuple, Optional
import numpy as np

try:
    from resilient_pid.controller.pid import DiscretePID
except ImportError:
    from pid import DiscretePID


class SmithPredictor:
    """
    Smith Predictor for network-delayed discrete control systems.

    Decouples transport latency from the closed-loop characteristic equation
    using an internal discrete plant model and an O(1) circular delay ring buffer.
    """

    def __init__(
        self,
        kp: float = 0.35,
        ki: float = 0.12,
        kd: float = 0.06,
        dt: float = 0.05,
        plant_delay_ms: float = 100.0,
        output_limits: Tuple[float, float] = (0.0, 100.0),
        derivative_filter_tau: float = 0.02
    ):
        """
        Initialize the Smith Predictor controller.

        Args:
            kp (float): Proportional gain.
            ki (float): Integral gain.
            kd (float): Derivative gain.
            dt (float): Sampling interval in seconds (default: 0.05s / 50ms).
            plant_delay_ms (float): Expected round-trip transport delay in milliseconds.
            output_limits (Tuple[float, float]): Saturation limits (min, max) for u(t).
            derivative_filter_tau (float): Low-pass derivative filter time constant.
        """
        self.kp = kp
        self.ki = ki
        self.kd = kd
        self.dt = dt
        self.output_limits = output_limits
        self.plant_delay_ms = plant_delay_ms

        # Encapsulated primary Discrete PID controller
        self.pid = DiscretePID(
            kp=self.kp,
            ki=self.ki,
            kd=self.kd,
            dt=self.dt,
            output_limits=self.output_limits,
            derivative_filter_tau=derivative_filter_tau
        )

        # Calibrated 2nd-order ARX discrete model coefficients (T_s = 50ms):
        # G_p0(z) = (b1*z^-1 + b2*z^-2) / (1 + a1*z^-1 + a2*z^-2)
        self.a1 = -1.6705
        self.a2 = 0.6967
        self.b1 = 0.0142
        self.b2 = 0.0121

        # Model state memory registers
        self.y_model_1 = 0.0
        self.y_model_2 = 0.0
        self.u_model_1 = 0.0
        self.u_model_2 = 0.0

        # Initialize circular delay ring buffer
        self.delay_steps = max(1, int(round(self.plant_delay_ms / (self.dt * 1000.0))))
        self.delay_buffer: deque[float] = deque([0.0] * self.delay_steps, maxlen=self.delay_steps)

    def set_dead_time(self, plant_delay_ms: float) -> None:
        """
        Dynamically update the delay buffer size based on empirical RTT feedback.

        Args:
            plant_delay_ms (float): Updated transport delay in milliseconds.
        """
        self.plant_delay_ms = plant_delay_ms
        self.delay_steps = max(1, int(round(self.plant_delay_ms / (self.dt * 1000.0))))
        self.delay_buffer = deque([0.0] * self.delay_steps, maxlen=self.delay_steps)

    def reset(self) -> None:
        """Reset primary PID integrators, model state history, and delay queue."""
        self.pid.reset()
        self.y_model_1 = 0.0
        self.y_model_2 = 0.0
        self.u_model_1 = 0.0
        self.u_model_2 = 0.0
        self.delay_buffer = deque([0.0] * self.delay_steps, maxlen=self.delay_steps)

    def update(self, setpoint: float, pv: float, is_loss: bool = False) -> float:
        """
        Calculates dead-time compensated control effort u[k].

        Args:
            setpoint (float): Target reference value SP[k].
            pv (float): Measured process variable feedback PV[k].
            is_loss (bool): Transmission loss flag (unused in Smith Predictor,
                            retained for polymorphic API compliance).

        Returns:
            float: Saturated control effort u[k] in range output_limits.
        """
        # 1. Retrieve the delayed model prediction from the head of the ring buffer
        y_model_delayed = self.delay_buffer[0] if len(self.delay_buffer) > 0 else 0.0

        # 2. Advance the un-delayed 2nd-order discrete plant model prediction:
        #    y_fast[k] = -a1*y[k-1] - a2*y[k-2] + b1*u[k-1] + b2*u[k-2]
        y_model_fast = (
            -self.a1 * self.y_model_1
            - self.a2 * self.y_model_2
            + self.b1 * self.u_model_1
            + self.b2 * self.u_model_2
        )

        # 3. Compute Smith feedback signal:
        #    y_pred[k] = y_fast[k] + (pv_actual[k] - y_delayed[k])
        model_mismatch = pv - y_model_delayed
        smith_feedback = y_model_fast + model_mismatch

        # 4. Primary Discrete PID update acting on un-delayed feedback signal
        u_t = self.pid.update(setpoint=setpoint, pv=smith_feedback, is_loss=is_loss)

        # 5. Push newest fast model prediction into delay ring buffer
        self.delay_buffer.append(y_model_fast)

        # 6. Shift internal model state registers
        self.y_model_2 = self.y_model_1
        self.y_model_1 = y_model_fast
        self.u_model_2 = self.u_model_1
        self.u_model_1 = u_t

        return u_t
