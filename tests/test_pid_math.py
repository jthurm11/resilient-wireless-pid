#!/usr/bin/env python3
"""
test_pid_math.py
Unit tests verifying control mathematics, delay buffers, anti-windup clamping,
and loss-hold logic across DiscretePID, SmithPredictor, and ResilientPID.
"""

import os
import sys
import unittest

# Ensure package or standalone import resolution
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from resilient_pid.controller.arx_model import ARXModelParams
    from resilient_pid.controller.pid import DiscretePID, PIDConfig
    from resilient_pid.controller.resilient_pid import ResilientPID
    from resilient_pid.controller.smith_predictor import SmithPredictor
except ImportError:
    from arx_model import ARXModelParams
    from pid import DiscretePID, PIDConfig
    from smith_predictor import SmithPredictor

    from resilient_pid import ResilientPID


class TestControlMathematics(unittest.TestCase):
    """Mathematical and interface regression tests for controller classes."""

    # =========================================================================
    # 1. DiscretePID Tests
    # =========================================================================

    def test_discrete_pid_hover_feedforward_at_zero_error(self):
        """Verify that with zero error and no integral, output equals hover bias."""
        cfg = PIDConfig(ff_hover=38.5, kp=0.17, ki=0.02, kd=0.13)
        pid = DiscretePID(config=cfg)

        # SP = 50.0, PV = 50.0 -> error = 0, dPV = 0, integral = 0
        u_out = pid.update(setpoint=50.0, pv=50.0, is_loss=False)
        self.assertAlmostEqual(u_out, 38.5, places=2)

    def test_discrete_pid_conditional_integration_floor_gate(self):
        """Verify integrator does not wind up while the ball rests on the floor mesh (PV < 12%)."""
        pid = DiscretePID()

        # Command lift while ball is at floor (PV = 5.0%)
        for _ in range(50):
            pid.update(setpoint=50.0, pv=5.0, is_loss=False)

        # Integration must remain zero to prevent the 65% launch rocket
        self.assertEqual(pid.integral, 0.0)

    def test_discrete_pid_conditional_integration_ceiling_gate(self):
        """Verify integrator does not wind up while the ball is at the top aperture (PV > 88%)."""
        pid = DiscretePID()

        # Ball forced into top stop (PV = 92.0%)
        for _ in range(50):
            pid.update(setpoint=50.0, pv=92.0, is_loss=False)

        self.assertEqual(pid.integral, 0.0)

    def test_discrete_pid_anti_windup_clamping(self):
        """Verify that integrator accumulation bounds strictly at integral_limits."""
        cfg = PIDConfig(
            ff_hover=38.5,
            integral_band=10.0,
            integral_limits=(-12.0, 12.0),
            output_limits=(33.5, 58.0),
            ki=1.0,  # High gain to force fast integration
        )
        pid = DiscretePID(config=cfg)

        # Drive steady error inside the valid band: SP=50, PV=45 -> error = +5.0
        for _ in range(100):
            pid.update(setpoint=50.0, pv=45.0, is_loss=False)

        # Integrator must be clamped to the symmetric boundary (+12.0)
        self.assertAlmostEqual(pid.integral, 12.0, places=2)
        # Actuator output must respect upper flight ceiling (58.0%)
        self.assertLessEqual(pid.update(setpoint=50.0, pv=45.0), 58.0)

    def test_discrete_pid_loss_flag_suspends_integration(self):
        """Verify that is_loss=True freezes integral state inside the linear band."""
        cfg = PIDConfig(integral_band=10.0)
        pid = DiscretePID(config=cfg)

        # Nominal step inside integration envelope: SP=50.0, PV=48.0
        pid.update(setpoint=50.0, pv=48.0, is_loss=False)
        integral_nominal = pid.integral
        self.assertGreater(integral_nominal, 0.0)

        # Subsequent step with loss flag set
        pid.update(setpoint=50.0, pv=48.0, is_loss=True)
        self.assertEqual(pid.integral, integral_nominal)

    def test_discrete_pid_shutdown_zeroes_actuator(self):
        """Verify commanding SP <= 0 overrides the flight floor clamp and returns 0.0%."""
        pid = DiscretePID()
        pid.update(setpoint=50.0, pv=50.0)

        u_stop = pid.update(setpoint=0.0, pv=50.0)
        self.assertEqual(u_stop, 0.0)
        self.assertEqual(pid.integral, 0.0)

    def test_discrete_pid_reset(self):
        """Verify that reset clears all internal state registers to zero."""
        pid = DiscretePID()
        pid.update(setpoint=50.0, pv=48.0)

        pid.reset()
        self.assertEqual(pid.integral, 0.0)
        self.assertIsNone(pid.prev_pv)
        self.assertEqual(pid.filtered_derivative, 0.0)

    # =========================================================================
    # 2. SmithPredictor Tests
    # =========================================================================

    def test_smith_predictor_delay_buffer_sizing(self):
        """Verify circular delay buffer capacity matches target dead time steps."""
        dt = 0.05
        plant_delay_ms = 200.0
        cfg = PIDConfig(dt=dt)
        arx = ARXModelParams()

        sp = SmithPredictor(config=cfg, model_params=arx, plant_delay_ms=plant_delay_ms)
        expected_steps = int(round(plant_delay_ms / (dt * 1000.0)))

        self.assertEqual(sp.delay_steps, expected_steps)
        self.assertEqual(len(sp.delay_buffer), expected_steps)

    def test_smith_predictor_dynamic_dead_time_adjustment(self):
        """Verify dynamic reconfiguration of delay buffer depth."""
        cfg = PIDConfig(dt=0.05)
        arx = ARXModelParams()

        sp = SmithPredictor(config=cfg, model_params=arx, plant_delay_ms=100.0)
        self.assertEqual(sp.delay_steps, 2)

        sp.set_dead_time(200.0)
        self.assertEqual(sp.delay_steps, 4)
        self.assertEqual(len(sp.delay_buffer), 4)

    def test_smith_predictor_reset(self):
        """Verify predictor reset purges model registers and queue history."""
        cfg = PIDConfig(dt=0.05)
        arx = ARXModelParams()

        sp = SmithPredictor(config=cfg, model_params=arx, plant_delay_ms=100.0)
        sp.update(setpoint=50.0, pv=48.0)

        sp.reset()
        self.assertEqual(sp.model.y_1, 0.0)
        self.assertEqual(sp.model.y_2, 0.0)
        self.assertEqual(sp.model.u_1, 0.0)
        self.assertEqual(sp.model.u_2, 0.0)
        self.assertTrue(all(val == 0.0 for val in sp.delay_buffer))

    # =========================================================================
    # 3. ResilientPID Tests
    # =========================================================================

    def test_resilient_pid_nominal_execution(self):
        """Verify nominal tracking step updates observer state."""
        cfg = PIDConfig(kp=0.35, ki=0.12, kd=0.06, dt=0.05)
        arx = ARXModelParams()

        rpid = ResilientPID(config=cfg, model_params=arx)
        u_out = rpid.update(setpoint=50.0, pv=48.0, is_loss=False)

        # Must respect active operational envelope
        self.assertTrue(33.5 <= u_out <= 58.0)
        self.assertEqual(rpid.consecutive_losses, 0)
        self.assertNotEqual(rpid.y_est, 0.0)

    def test_resilient_pid_dropout_compensation(self):
        """Verify observer feedback substitution during packet loss."""
        cfg = PIDConfig(kp=0.35, ki=0.12, kd=0.06, dt=0.05)
        arx = ARXModelParams()

        rpid = ResilientPID(config=cfg, model_params=arx)
        rpid.update(setpoint=50.0, pv=48.0, is_loss=False)

        u_loss = rpid.update(setpoint=50.0, pv=0.0, is_loss=True)
        self.assertEqual(rpid.consecutive_losses, 1)
        self.assertTrue(33.5 <= u_loss <= 58.0)

    def test_resilient_pid_reset(self):
        """Verify resilient controller reset clears registers and loss tally."""
        cfg = PIDConfig(kp=0.35, ki=0.12, kd=0.06, dt=0.05)
        arx = ARXModelParams()

        rpid = ResilientPID(config=cfg, model_params=arx)
        rpid.update(setpoint=50.0, pv=48.0, is_loss=True)

        rpid.reset()
        self.assertEqual(rpid.consecutive_losses, 0)
        self.assertEqual(rpid.y_est, 0.0)
        self.assertEqual(rpid.model.y_1, 0.0)

    # =========================================================================
    # 4. Polymorphic API Contract Test
    # =========================================================================

    def test_polymorphic_controller_interface(self):
        """Verify unified update and reset invocation contracts across all controllers."""
        cfg = PIDConfig(dt=0.05)
        arx = ARXModelParams()

        controllers = [
            DiscretePID(config=cfg),
            SmithPredictor(config=cfg, model_params=arx, plant_delay_ms=100.0),
            ResilientPID(config=cfg, model_params=arx),
        ]

        for ctrl in controllers:
            ctrl.reset()
            u_norm = ctrl.update(setpoint=50.0, pv=48.0, is_loss=False)
            self.assertIsInstance(u_norm, float)
            self.assertGreaterEqual(u_norm, 33.5)
            self.assertLessEqual(u_norm, 58.0)

            u_loss = ctrl.update(setpoint=50.0, pv=48.0, is_loss=True)
            self.assertIsInstance(u_loss, float)
            self.assertGreaterEqual(u_loss, 33.5)
            self.assertLessEqual(u_loss, 58.0)


if __name__ == "__main__":
    unittest.main()
