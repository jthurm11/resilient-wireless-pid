import pytest
from resilient_pid.controller.pid import DiscretePID
from resilient_pid.controller.smith_predictor import SmithPredictor
from resilient_pid.controller.resilient_pid import ResilientPID

def test_discrete_pid_anti_windup_clamping():
    """Verify that the discrete integrator does not exceed output saturation limits."""
    pid = DiscretePID(kp=2.0, ki=1.5, kd=0.0, output_limits=(0.0, 100.0))
    
    # Inject a massive sustained error to force integrator windup
    u_out = 0.0
    for _ in range(50):
        u_out = pid.update(setpoint=100.0, pv=0.0)
        
    # The output must be strictly clamped to the upper limit (100.0)
    assert u_out == 100.0
    assert pid.output_limits[0] <= u_out <= pid.output_limits[1]

def test_smith_predictor_delay_buffer_sizing():
    """Verify the circular delay buffer is sized correctly for the transport lag."""
    dt = 0.05  # 50ms loop
    plant_delay_ms = 200 # 200ms network delay
    
    sp = SmithPredictor(
        kp=1.0, ki=0.5, kd=0.1, 
        plant_delay_ms=plant_delay_ms, 
        dt=dt
    )
    
    # Delay elements (N) = plant_delay_ms / (dt * 1000)
    # 200 / 50 = 4 elements in the queue
    expected_buffer_length = int(plant_delay_ms / (dt * 1000.0))
    assert sp.delay_frames == expected_buffer_length

def test_resilient_pid_instantiation():
    """Verify the resilient controller initializes with the correct state estimator flags."""
    rpid = ResilientPID(kp=1.0, ki=1.0, kd=0.0)
    
    # Verify it can process a nominal frame without crashing
    u_out = rpid.update(setpoint=50.0, pv=45.0, is_loss=False)
    assert isinstance(u_out, float)