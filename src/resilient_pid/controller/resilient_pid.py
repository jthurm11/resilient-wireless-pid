#!/usr/bin/env python3
"""
src/resilient_pid/controller/resilient_pid.py
==============================================

Resilient / Event-Triggered Observer PID Controller implementation for 
hardening control loops against packet dropouts and loss bursts.

Project: Resilient Wireless Control: Hardening PID Loops against Network Jitter
Task 4.2: Resilient Observer & Dropout Compensation Engine

Mathematical Formulation:
-------------------------
When network transmission succeeds (is_loss = False):
  1. The controller uses the measured process variable PV_actual[k].
  2. The internal predictive state observer resynchronizes its state to PV_actual[k].
  3. Control effort u[k] is calculated using DiscretePID.
  4. The 2nd-order discrete model advances to estimate PV_est[k+1]:
     y_next = -a1*y_model_1 - a2*y_model_2 + b1*u_model_1 + b2*u_model_2

When network packet loss occurs (is_loss = True):
  1. The controller uses the observer prediction y_est[k] as feedback.
  2. DiscretePID operates with is_loss=True to suspend integral windup.
  3. Control effort u[k] is computed using the estimated feedback y_est[k].
  4. The observer advances model state registers for the next cycle.
"""

from typing import Tuple, Optional

try:
    from resilient_pid.controller.pid import DiscretePID
except ImportError:
    from pid import DiscretePID


class ResilientPID:
    """
    Resilient Observer PID Controller.

    Combines a primary Discrete PID with an internal 2nd-order autoregressive (ARX)
    predictive state observer to maintain stable control actuation during network
    packet dropouts and burst losses.
    """

    def __init__(
        self,
        kp: float = 0.35,
        ki: float = 0.12,
        kd: float = 0.06,
        dt: float = 0.05,
        output_limits: Tuple[float, float] = (0.0, 100.0),
        derivative_filter_tau: float = 0.02
    ):
        """
        Initialize the Resilient Observer PID controller.

        Args:
            kp (float): Proportional gain.
            ki (float): Integral gain.
            kd (float): Derivative gain.
            dt (float): Sampling period in seconds (default: 0.05s / 50ms).
            output_limits (Tuple[float, float]): Saturation bounds (min, max).
            derivative_filter_tau (float): Time constant for derivative filter.
        """
        self.kp = kp
        self.ki = ki
        self.kd = kd
        self.dt = dt
        self.output_limits = output_limits

        # Primary Discrete PID instance
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

        # Observer state registers
        self.y_est = 0.0
        self.y_model_1 = 0.0
        self.y_model_2 = 0.0
        self.u_model_1 = 0.0
        self.u_model_2 = 0.0

        # Loss tracking diagnostics
        self.consecutive_losses = 0

    def reset(self) -> None:
        """Reset PID integrators, observer states, and loss counters."""
        self.pid.reset()
        self.y_est = 0.0
        self.y_model_1 = 0.0
        self.y_model_2 = 0.0
        self.u_model_1 = 0.0
        self.u_model_2 = 0.0
        self.consecutive_losses = 0

    def update(
        self,
        setpoint: float,
        pv: float = 0.0,
        is_loss: bool = False,
        pv_actual: Optional[float] = None
    ) -> float:
        """
        Calculate control effort u[k] with dropout compensation.

        Args:
            setpoint (float): Target reference SP[k].
            pv (float): Measured process variable PV[k] (default: 0.0).
            is_loss (bool): Packet loss indicator flag.
            pv_actual (Optional[float]): Alias for actual process variable.

        Returns:
            float: Bounded control output u[k].
        """
        # Resolve actual measured PV (support positional or keyword aliasing)
        measured_pv = pv_actual if pv_actual is not None else pv

        if not is_loss:
            self.consecutive_losses = 0
            # 1. Normal packet reception: Feedback comes from actual sensor measurement
            feedback_pv = measured_pv
            self.y_model_1 = measured_pv

            # 2. Update primary PID with real sensor measurement
            u_t = self.pid.update(setpoint=setpoint, pv=feedback_pv, is_loss=False)

        else:
            self.consecutive_losses += 1
            # 1. Packet dropout: Feedback comes from internal predictive observer
            feedback_pv = self.y_est

            # 2. Update primary PID with estimated PV and loss flag (suspends integral windup)
            u_t = self.pid.update(setpoint=setpoint, pv=feedback_pv, is_loss=True)

        # 3. Advance discrete 2nd-order model state prediction for step k+1
        y_next = (
            -self.a1 * self.y_model_1
            - self.a2 * self.y_model_2
            + self.b1 * self.u_model_1
            + self.b2 * self.u_model_2
        )

        # 4. Shift model state registers
        self.y_model_2 = self.y_model_1
        self.y_model_1 = y_next if is_loss else measured_pv
        self.u_model_2 = self.u_model_1
        self.u_model_1 = u_t

        # 5. Store predicted state for next step
        self.y_est = float(y_next)

        return u_t
