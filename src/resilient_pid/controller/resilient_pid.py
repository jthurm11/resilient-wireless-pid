#!/usr/bin/env python3
"""
resilient_pid.py
Resilient predictive observer PID controller for mitigating packet dropouts.
Executes recursive ARX state estimation and integral freezing during loss events.
"""

try:
    from resilient_pid.controller.pid import DiscretePID
except ImportError:
    from .pid import DiscretePID


class ResilientPID:
    """
    Resilient observer PID controller.
    Switches to model prediction and halts integral windup during packet dropouts.
    """

    def __init__(
        self,
        kp: float = 0.35,
        ki: float = 0.12,
        kd: float = 0.06,
        dt: float = 0.05,
        output_limits: tuple[float, float] = (0.0, 100.0),
        derivative_filter_tau: float = 0.02,
    ):
        """
        Initialize resilient observer gains, limits, and internal model states.

        Args:
            kp (float): Proportional gain.
            ki (float): Integral gain.
            kd (float): Derivative gain.
            dt (float): Sampling period in seconds.
            output_limits (Tuple[float, float]): Output saturation limits (min, max).
            derivative_filter_tau (float): Time constant for derivative filter.
        """
        self.kp = kp
        self.ki = ki
        self.kd = kd
        self.dt = dt
        self.output_limits = output_limits

        # Primary baseline PID controller
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

        # Observer memory registers
        self.y_est = 0.0
        self.y_model_1 = 0.0
        self.y_model_2 = 0.0
        self.u_model_1 = 0.0
        self.u_model_2 = 0.0

        # Loss diagnostics counter
        self.consecutive_losses = 0

    def reset(self) -> None:
        """Reset internal PID state, model registers, and loss counter."""
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
        pv_actual: float | None = None,
    ) -> float:
        """
        Calculate control effort u[k] with dropout compensation.

        Args:
            setpoint (float): Reference target value SP[k].
            pv (float): Measured process variable feedback PV[k].
            is_loss (bool): Packet loss flag indicating missing sensor feedback.
            pv_actual (Optional[float]): Alias parameter for measured process variable.

        Returns:
            float: Saturated control effort u[k].
        """
        # Resolve positional vs keyword measured feedback
        measured_pv = pv_actual if pv_actual is not None else pv

        if not is_loss:
            self.consecutive_losses = 0
            # Resynchronize state observer to physical sensor measurement
            self.y_model_1 = measured_pv
            u_t = self.pid.update(setpoint=setpoint, pv=measured_pv, is_loss=False)
        else:
            self.consecutive_losses += 1
            # Substitute missing feedback with predictive observer estimate and freeze integrator
            u_t = self.pid.update(setpoint=setpoint, pv=self.y_est, is_loss=True)

        # One-step ahead recursive prediction: y[k+1] = -a1*y[k] - a2*y[k-1] + b1*u[k] + b2*u[k-1]
        y_next = (
            -self.a1 * self.y_model_1
            - self.a2 * self.y_model_2
            + self.b1 * self.u_model_1
            + self.b2 * self.u_model_2
        )

        # Shift discrete delay registers
        self.y_model_2 = self.y_model_1
        self.y_model_1 = y_next if is_loss else measured_pv
        self.u_model_2 = self.u_model_1
        self.u_model_1 = u_t
        self.y_est = float(y_next)

        return u_t
