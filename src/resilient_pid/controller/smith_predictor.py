#!/usr/bin/env python3
"""
smith_predictor.py
Discrete Smith Predictor engine for transport dead-time compensation.
Decouples network latency from the closed-loop characteristic equation.
"""

from collections import deque

try:
    from resilient_pid.controller.pid import DiscretePID
except ImportError:
    from pid import DiscretePID


class SmithPredictor:
    """
    Discrete Smith Predictor controller.
    Pairs a discrete ARX plant model with an O(1) circular ring buffer.
    """

    def __init__(
        self,
        kp: float = 0.35,
        ki: float = 0.12,
        kd: float = 0.06,
        dt: float = 0.05,
        plant_delay_ms: float = 100.0,
        output_limits: tuple[float, float] = (0.0, 100.0),
        derivative_filter_tau: float = 0.02,
    ):
        """
        Initialize predictor gains, discrete model coefficients, and delay queue.

        Args:
            kp (float): Proportional gain.
            ki (float): Integral gain.
            kd (float): Derivative gain.
            dt (float): Sampling interval in seconds.
            plant_delay_ms (float): Expected transport latency in milliseconds.
            output_limits (Tuple[float, float]): Actuator output bounds (min, max).
            derivative_filter_tau (float): Time constant for derivative filter.
        """
        self.kp = kp
        self.ki = ki
        self.kd = kd
        self.dt = dt
        self.output_limits = output_limits
        self.plant_delay_ms = plant_delay_ms

        # Embedded baseline controller
        self.pid = DiscretePID(
            kp=self.kp,
            ki=self.ki,
            kd=self.kd,
            dt=self.dt,
            output_limits=self.output_limits,
            derivative_filter_tau=derivative_filter_tau,
        )

        # Calibrated 2nd-order discrete ARX plant parameters (Ts = 50ms)
        self.a1 = -1.6705
        self.a2 = 0.6967
        self.b1 = 0.0142
        self.b2 = 0.0121

        # Model state memory registers
        self.y_model_1 = 0.0
        self.y_model_2 = 0.0
        self.u_model_1 = 0.0
        self.u_model_2 = 0.0

        # Circular delay queue for delayed plant estimation
        self.delay_steps = max(1, int(round(self.plant_delay_ms / (self.dt * 1000.0))))
        self.delay_buffer: deque[float] = deque(
            [0.0] * self.delay_steps, maxlen=self.delay_steps
        )

    def set_dead_time(self, plant_delay_ms: float) -> None:
        """
        Reconfigure circular buffer length to match new network delay.

        Args:
            plant_delay_ms (float): Updated transport delay in milliseconds.
        """
        self.plant_delay_ms = plant_delay_ms
        self.delay_steps = max(1, int(round(self.plant_delay_ms / (self.dt * 1000.0))))
        self.delay_buffer = deque([0.0] * self.delay_steps, maxlen=self.delay_steps)

    def reset(self) -> None:
        """Clear primary PID integrators, model state registers, and delay queue."""
        self.pid.reset()
        self.y_model_1 = 0.0
        self.y_model_2 = 0.0
        self.u_model_1 = 0.0
        self.u_model_2 = 0.0
        self.delay_buffer = deque([0.0] * self.delay_steps, maxlen=self.delay_steps)

    def update(self, setpoint: float, pv: float, is_loss: bool = False) -> float:
        """
        Calculate dead-time compensated control effort u[k].

        Args:
            setpoint (float): Reference target value SP[k].
            pv (float): Measured process variable feedback PV[k].
            is_loss (bool): Loss indicator flag for API polymorphism.

        Returns:
            float: Saturated control effort u[k].
        """
        # Retrieve oldest delayed model output from ring buffer
        y_model_delayed = self.delay_buffer[0] if len(self.delay_buffer) > 0 else 0.0

        # Discrete recursive ARX step: G_p0(z) un-delayed estimation
        y_model_fast = (
            -self.a1 * self.y_model_1
            - self.a2 * self.y_model_2
            + self.b1 * self.u_model_1
            + self.b2 * self.u_model_2
        )

        # Reconstruct delay-free feedback using physical measurement mismatch
        model_mismatch = pv - y_model_delayed
        smith_feedback = y_model_fast + model_mismatch

        # Calculate actuation effort from un-delayed predictive feedback
        u_t = self.pid.update(setpoint=setpoint, pv=smith_feedback, is_loss=is_loss)

        # Enqueue current fast prediction and advance input-output history
        self.delay_buffer.append(y_model_fast)
        self.y_model_2 = self.y_model_1
        self.y_model_1 = y_model_fast
        self.u_model_2 = self.u_model_1
        self.u_model_1 = u_t

        return u_t
