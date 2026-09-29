#!/usr/bin/env python3
"""
test_pid_math.py
Unit tests verifying control math, delay buffers, and loss hold logic.
Evaluates DiscretePID, SmithPredictor, and ResilientPID contracts.
"""

import os
import sys
import unittest

# Ensure package or standalone import resolution
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from resilient_pid.controller.pid import DiscretePID
    from resilient_pid.controller.resilient_pid import ResilientPID
    from resilient_pid.controller.smith_predictor import SmithPredictor
except ImportError:
    from pid import DiscretePID
    from smith_predictor import SmithPredictor

    from resilient_pid import ResilientPID


class TestControlMathematics(unittest.TestCase):
    """Mathematical and interface regression tests for controller classes."""

    # =========================================================================
    # 1. DiscretePID Tests
    # =========================================================================

    def test_discrete_pid_anti_windup_clamping(self):
        """Verify that integrator accumulation bounds at maximum saturation limits."""
        pid = DiscretePID(kp=2.0, ki=1.5, kd=0.1, dt=0.05, output_limits=(0.0, 100.0))

        # Drive output to hard upper saturation
        for _ in range(100):
            u_out = pid.update(setpoint=100.0, pv=0.0)

        self.assertEqual(u_out, 100.0)
        self.assertLess(pid.integral, 1000.0)

    def test_discrete_pid_loss_flag_suspends_integration(self):
        """Verify that is_loss=True suspends integral accumulation."""
        pid = DiscretePID(kp=1.0, ki=1.0, kd=0.0, dt=0.05, output_limits=(0.0, 100.0))

        # Execute nominal step followed by loss step
        pid.update(setpoint=50.0, pv=0.0, is_loss=False)
        integral_nominal = pid.integral

        pid.update(setpoint=50.0, pv=0.0, is_loss=True)
        self.assertEqual(pid.integral, integral_nominal)

    def test_discrete_pid_reset(self):
        """Verify that reset clears all internal state registers."""
        pid = DiscretePID(kp=1.0, ki=1.0, kd=0.5, dt=0.05)
        pid.update(setpoint=50.0, pv=10.0)

        pid.reset()
        self.assertEqual(pid.integral, 0.0)
        self.assertEqual(pid.prev_error, 0.0)
        self.assertEqual(pid.filtered_derivative, 0.0)

    # =========================================================================
    # 2. SmithPredictor Tests
    # =========================================================================

    def test_smith_predictor_delay_buffer_sizing(self):
        """Verify circular delay buffer capacity matches target dead time steps."""
        dt = 0.05
        plant_delay_ms = 200.0

        sp = SmithPredictor(dt=dt, plant_delay_ms=plant_delay_ms)
        expected_steps = int(round(plant_delay_ms / (dt * 1000.0)))

        self.assertEqual(sp.delay_steps, expected_steps)
        self.assertEqual(len(sp.delay_buffer), expected_steps)

    def test_smith_predictor_dynamic_dead_time_adjustment(self):
        """Verify dynamic reconfiguration of delay buffer depth."""
        sp = SmithPredictor(dt=0.05, plant_delay_ms=100.0)
        self.assertEqual(sp.delay_steps, 2)

        sp.set_dead_time(200.0)
        self.assertEqual(sp.delay_steps, 4)
        self.assertEqual(len(sp.delay_buffer), 4)

    def test_smith_predictor_reset(self):
        """Verify predictor reset purges model registers and queue history."""
        sp = SmithPredictor(dt=0.05, plant_delay_ms=100.0)
        sp.update(setpoint=50.0, pv=10.0)

        sp.reset()
        self.assertEqual(sp.y_model_1, 0.0)
        self.assertEqual(sp.y_model_2, 0.0)
        self.assertEqual(sp.u_model_1, 0.0)
        self.assertEqual(sp.u_model_2, 0.0)
        self.assertTrue(all(val == 0.0 for val in sp.delay_buffer))

    # =========================================================================
    # 3. ResilientPID Tests
    # =========================================================================

    def test_resilient_pid_nominal_execution(self):
        """Verify nominal tracking step updates observer state."""
        rpid = ResilientPID(kp=0.35, ki=0.12, kd=0.06, dt=0.05)
        u_out = rpid.update(setpoint=50.0, pv=20.0, is_loss=False)

        self.assertTrue(0.0 <= u_out <= 100.0)
        self.assertEqual(rpid.consecutive_losses, 0)
        self.assertNotEqual(rpid.y_est, 0.0)

    def test_resilient_pid_dropout_compensation(self):
        """Verify observer feedback substitution during packet loss."""
        rpid = ResilientPID(kp=0.35, ki=0.12, kd=0.06, dt=0.05)
        rpid.update(setpoint=50.0, pv=20.0, is_loss=False)

        u_loss = rpid.update(setpoint=50.0, pv=0.0, is_loss=True)
        self.assertEqual(rpid.consecutive_losses, 1)
        self.assertTrue(0.0 <= u_loss <= 100.0)

    def test_resilient_pid_reset(self):
        """Verify resilient controller reset clears registers and loss tally."""
        rpid = ResilientPID(kp=0.35, ki=0.12, kd=0.06, dt=0.05)
        rpid.update(setpoint=50.0, pv=20.0, is_loss=True)

        rpid.reset()
        self.assertEqual(rpid.consecutive_losses, 0)
        self.assertEqual(rpid.y_est, 0.0)
        self.assertEqual(rpid.y_model_1, 0.0)

    # =========================================================================
    # 4. Polymorphic API Contract Test
    # =========================================================================

    def test_polymorphic_controller_interface(self):
        """Verify unified update and reset invocation contracts across all controllers."""
        controllers = [
            DiscretePID(dt=0.05),
            SmithPredictor(dt=0.05, plant_delay_ms=100.0),
            ResilientPID(dt=0.05),
        ]

        for ctrl in controllers:
            ctrl.reset()
            u_norm = ctrl.update(setpoint=50.0, pv=25.0, is_loss=False)
            self.assertIsInstance(u_norm, float)

            u_loss = ctrl.update(setpoint=50.0, pv=25.0, is_loss=True)
            self.assertIsInstance(u_loss, float)


if __name__ == "__main__":
    unittest.main()
