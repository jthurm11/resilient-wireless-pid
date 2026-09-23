#!/usr/bin/env python3
"""
tests/test_pid_math.py
======================

Comprehensive unit test suite for control algorithm mathematical accuracy,
anti-windup clamping, Smith Predictor dead-time compensation, and
Resilient Observer dropout hold logic.

Project: Resilient Wireless Control: Hardening PID Loops against Network Jitter
"""

import sys
import os
import unittest
from collections import deque

# Ensure current directory is in sys.path for standalone or package imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from resilient_pid.controller.pid import DiscretePID
    from resilient_pid.controller.smith_predictor import SmithPredictor
    from resilient_pid.controller.resilient_pid import ResilientPID
except ImportError:
    from pid import DiscretePID
    from smith_predictor import SmithPredictor
    from resilient_pid import ResilientPID


class TestControlMathematics(unittest.TestCase):
    """Unit tests for DiscretePID, SmithPredictor, and ResilientPID."""

    # =========================================================================
    # 1. DiscretePID Tests
    # =========================================================================

    def test_discrete_pid_anti_windup_clamping(self):
        """Verify that discrete PID anti-windup prevents integrator runaway when saturated."""
        pid = DiscretePID(kp=2.0, ki=1.5, kd=0.1, dt=0.05, output_limits=(0.0, 100.0))

        # Inject a massive sustained setpoint error to force output saturation
        for _ in range(100):
            u_out = pid.update(setpoint=100.0, pv=0.0)

        # Output must be strictly bounded by max limit
        self.assertEqual(u_out, 100.0)

        # Integrator should not accumulate endlessly due to anti-windup clamping
        self.assertLess(pid.integral, 1000.0)

    def test_discrete_pid_loss_flag_suspends_integration(self):
        """Verify that setting is_loss=True suspends integral accumulation in DiscretePID."""
        pid = DiscretePID(kp=1.0, ki=1.0, kd=0.0, dt=0.05, output_limits=(0.0, 100.0))

        # Single normal step
        pid.update(setpoint=50.0, pv=0.0, is_loss=False)
        integral_before = pid.integral

        # Loss step with error
        pid.update(setpoint=50.0, pv=0.0, is_loss=True)
        integral_after = pid.integral

        # Integral accumulation should be suspended during packet loss
        self.assertEqual(integral_after, integral_before)

    def test_discrete_pid_reset(self):
        """Verify that reset() clears all state registers."""
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
        """Verify circular delay buffer is sized correctly for the specified dead time."""
        dt = 0.05             # 50 ms loop
        plant_delay_ms = 200  # 200 ms network lag -> 200 / 50 = 4 steps

        sp = SmithPredictor(
            kp=0.35, ki=0.12, kd=0.06,
            dt=dt, plant_delay_ms=plant_delay_ms,
            output_limits=(0.0, 100.0)
        )

        expected_steps = int(round(plant_delay_ms / (dt * 1000.0)))
        self.assertEqual(sp.delay_steps, expected_steps)
        self.assertEqual(len(sp.delay_buffer), expected_steps)

    def test_smith_predictor_dynamic_dead_time_adjustment(self):
        """Verify set_dead_time updates ring buffer length dynamically."""
        sp = SmithPredictor(dt=0.05, plant_delay_ms=100.0)
        self.assertEqual(sp.delay_steps, 2)

        sp.set_dead_time(200.0)
        self.assertEqual(sp.delay_steps, 4)
        self.assertEqual(len(sp.delay_buffer), 4)

    def test_smith_predictor_reset(self):
        """Verify SmithPredictor reset clears state registers and buffer."""
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
        """Verify ResilientPID calculates output and updates observer prediction during normal operation."""
        rpid = ResilientPID(kp=0.35, ki=0.12, kd=0.06, dt=0.05)

        u_out = rpid.update(setpoint=50.0, pv=20.0, is_loss=False)

        self.assertTrue(0.0 <= u_out <= 100.0)
        self.assertEqual(rpid.consecutive_losses, 0)
        self.assertNotEqual(rpid.y_est, 0.0)

    def test_resilient_pid_dropout_compensation(self):
        """Verify ResilientPID uses predictive state estimation during packet dropouts."""
        rpid = ResilientPID(kp=0.35, ki=0.12, kd=0.06, dt=0.05)

        # Initial normal step
        rpid.update(setpoint=50.0, pv=20.0, is_loss=False)

        # Loss step (is_loss=True)
        u_loss = rpid.update(setpoint=50.0, pv=0.0, is_loss=True)

        self.assertEqual(rpid.consecutive_losses, 1)
        self.assertTrue(0.0 <= u_loss <= 100.0)

    def test_resilient_pid_reset(self):
        """Verify ResilientPID reset clears observer and loss counters."""
        rpid = ResilientPID(kp=0.35, ki=0.12, kd=0.06, dt=0.05)
        rpid.update(setpoint=50.0, pv=20.0, is_loss=True)

        rpid.reset()
        self.assertEqual(rpid.consecutive_losses, 0)
        self.assertEqual(rpid.y_est, 0.0)
        self.assertEqual(rpid.y_model_1, 0.0)

    # =========================================================================
    # 4. Polymorphic API Compliance Test
    # =========================================================================

    def test_polymorphic_controller_interface(self):
        """Verify DiscretePID, SmithPredictor, and ResilientPID share unified polymorphic call contract."""
        dt = 0.05
        controllers = [
            DiscretePID(kp=0.35, ki=0.12, kd=0.06, dt=dt),
            SmithPredictor(kp=0.35, ki=0.12, kd=0.06, dt=dt, plant_delay_ms=100.0),
            ResilientPID(kp=0.35, ki=0.12, kd=0.06, dt=dt)
        ]

        for ctrl in controllers:
            ctrl.reset()
            # Normal step
            u_norm = ctrl.update(setpoint=50.0, pv=25.0, is_loss=False)
            self.assertIsInstance(u_norm, float)
            self.assertTrue(0.0 <= u_norm <= 100.0)

            # Loss step
            u_loss = ctrl.update(setpoint=50.0, pv=25.0, is_loss=True)
            self.assertIsInstance(u_loss, float)
            self.assertTrue(0.0 <= u_loss <= 100.0)


if __name__ == "__main__":
    unittest.main()
