#!/usr/bin/env python3
"""
test_pid_math.py
Unit tests verifying control mathematics, delay buffers, anti-windup clamping,
takeoff windup discharge, and loss-hold logic across DiscretePID,
SmithPredictor, and ResilientPID.
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
        cfg = PIDConfig(ff_hover=33.5, kp=0.16, ki=0.022, kd=0.15)
        pid = DiscretePID(config=cfg)

        # SP = 50.0, PV = 50.0 -> error = 0, dPV = 0, integral = 0
        u_out = pid.update(setpoint=50.0, pv=50.0, is_loss=False)
        self.assertAlmostEqual(u_out, 33.5, places=2)

    def test_discrete_pid_debounced_floor_arming_and_takeoff_discharge(self):
        """Verify 3-frame debounce arms takeoff, and liftoff (PV >= 16%) discharges integral."""
        pid = DiscretePID()

        # Frame 1 & 2 on mesh: Not yet armed
        pid.update(setpoint=50.0, pv=8.0, is_loss=False)
        self.assertFalse(pid._liftoff_armed)
        pid.update(setpoint=50.0, pv=8.0, is_loss=False)
        self.assertFalse(pid._liftoff_armed)

        # Frame 3 on mesh: Takeoff state is armed
        pid.update(setpoint=50.0, pv=8.0, is_loss=False)
        self.assertTrue(pid._liftoff_armed)
        self.assertGreater(pid.integral, 0.0)

        # Allow takeoff integrator to accumulate upward authority (20 ticks @ ~0.294%/tick)
        for _ in range(20):
            pid.update(setpoint=50.0, pv=8.0, is_loss=False)
        self.assertGreater(pid.integral, 5.0)

        # Ball crosses liftoff threshold (PV >= 16%): Must discharge accumulated takeoff integral
        u_liftoff = pid.update(setpoint=50.0, pv=18.0, is_loss=False)
        self.assertEqual(pid.integral, 0.0)
        self.assertFalse(pid._liftoff_armed)
        # Output should throttle down to hover corridor (< 45%)
        self.assertLess(u_liftoff, 45.0)

    def test_discrete_pid_ceiling_suction_escape(self):
        """Verify that ball entering the ceiling zone (PV >= 80%) relieves suction and bounds output."""
        pid = DiscretePID()

        # Ball forced into top stop: SP=50, PV=88.0
        u_out = pid.update(setpoint=50.0, pv=88.0, is_loss=False)

        # Output must drop below hover equilibrium (33.5%) to allow smooth descent
        self.assertLess(u_out, 30.0)
        self.assertGreaterEqual(u_out, 18.0)
        self.assertLessEqual(pid.integral, 0.0)

    def test_discrete_pid_anti_windup_clamping(self):
        """Verify that integrator accumulation bounds strictly at integral_limits."""
        cfg = PIDConfig(
            ff_hover=33.5,
            integral_limits=(-12.0, 30.0),
            output_limits=(20.0, 75.0),
            ki=2.0,  # High gain to force fast integration
        )
        pid = DiscretePID(config=cfg)

        # Drive steady positive error inside the active flight corridor (SP=50, PV=45)
        for _ in range(100):
            pid.update(setpoint=50.0, pv=45.0, is_loss=False)

        # Integrator must be clamped at the upper limit (30.0)
        self.assertAlmostEqual(pid.integral, 30.0, places=2)
        # Actuator output must respect upper limit (75.0)
        self.assertLessEqual(pid.update(setpoint=50.0, pv=45.0), 75.0)

    def test_discrete_pid_loss_flag_suspends_integration(self):
        """Verify that is_loss=True freezes integral state inside the linear band."""
        pid = DiscretePID()

        # Active flight step: SP=50.0, PV=48.0
        pid.update(setpoint=50.0, pv=48.0, is_loss=False)
        integral_nominal = pid.integral
        self.assertGreater(integral_nominal, 0.0)

        # Subsequent step with loss flag set: Integral must not change
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
        """Verify that reset clears all internal state registers and armed flags."""
        pid = DiscretePID()
        pid.update(setpoint=50.0, pv=8.0)

        pid.reset()
        self.assertEqual(pid.integral, 0.0)
        self.assertIsNone(pid.prev_pv)
        self.assertEqual(pid.filtered_derivative, 0.0)
        self.assertFalse(pid._liftoff_armed)
        self.assertEqual(pid._floor_confirm_count, 0)

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
        """Verify nominal tracking step updates observer state within operational bounds."""
        cfg = PIDConfig(kp=0.35, ki=0.12, kd=0.06, dt=0.05)
        arx = ARXModelParams()

        rpid = ResilientPID(config=cfg, model_params=arx)
        u_out = rpid.update(setpoint=50.0, pv=48.0, is_loss=False)

        # Operational envelope check
        self.assertTrue(8.0 <= u_out <= 75.0)
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
        # Bounded between safe dropout/ceiling floor (8.0%) and maximum thrust (75.0%)
        self.assertTrue(8.0 <= u_loss <= 75.0)

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
            self.assertGreaterEqual(u_norm, 8.0)
            self.assertLessEqual(u_norm, 75.0)

            u_loss = ctrl.update(setpoint=50.0, pv=48.0, is_loss=True)
            self.assertIsInstance(u_loss, float)
            self.assertGreaterEqual(u_loss, 8.0)
            self.assertLessEqual(u_loss, 75.0)


if __name__ == "__main__":
    unittest.main()
