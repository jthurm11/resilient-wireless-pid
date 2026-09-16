from resilient_pid.controller.pid import DiscretePID


class ResilientPID:
    """
    Resilient PID Controller utilizing a predictive linear observer
    and zero-order hold (ZOH) state recovery during network dropouts.
    """

    def __init__(
        self,
        kp: float,
        ki: float,
        kd: float,
        dt: float,
        output_limits: tuple[float, float] = (0.0, 100.0),
    ):
        self.pid = DiscretePID(kp, ki, kd, dt, output_limits)
        self.dt = dt

        self.a1 = -1.6705
        self.a2 = 0.6967
        self.b1 = 0.0142
        self.b2 = 0.0121

        self.y_est_1 = 0.0
        self.y_est_2 = 0.0
        self.u_est_1 = 0.0
        self.u_est_2 = 0.0
        self.y_est = 0.0

    def reset(self) -> None:
        self.pid.reset()
        self.y_est_1 = 0.0
        self.y_est_2 = 0.0
        self.u_est_1 = 0.0
        self.u_est_2 = 0.0
        self.y_est = 0.0

    def update(self, setpoint: float, pv_actual: float, is_loss: bool = False) -> float:
        if is_loss:
            # Observer propagation: compute step prediction from past state memory
            self.y_est = (
                -self.a1 * self.y_est_1
                - self.a2 * self.y_est_2
                + self.b1 * self.u_est_1
                + self.b2 * self.u_est_2
            )
            feedback_pv = self.y_est
        else:
            self.y_est = pv_actual
            feedback_pv = pv_actual

        u_t = self.pid.update(setpoint, feedback_pv)

        # Shift observer history
        self.y_est_2 = self.y_est_1
        self.y_est_1 = feedback_pv
        self.u_est_2 = self.u_est_1
        self.u_est_1 = u_t

        return u_t
