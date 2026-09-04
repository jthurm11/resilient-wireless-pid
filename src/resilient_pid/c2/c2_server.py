import os
import time
import logging
import threading
from typing import Optional, Dict, Any
from flask import Flask, jsonify, request
from flask_cors import CORS

# Import our custom components (assumes packaged structure or same directory)
try:
    from resilient_pid.emulation.traffic_control import TrafficControlWrapper
    from resilient_pid.telemetry.influx_writer import InfluxWriter
except ImportError:
    # Fallback to local import for standalone execution
    from traffic_control import TrafficControlWrapper
    from influx_writer import InfluxWriter

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("C2Server")

app = Flask(__name__)
# Enable CORS for the React Operator Dashboard (usually running on port 3000)
CORS(app, resources={r"/api/*": {"origins": "*"}})

# Global operational state of the Hardening Framework
class TrialState:
    def __init__(self):
        self.is_running = False
        self.trial_id: Optional[str] = None
        self.algorithm: str = "standard_pid"
        self.setpoint: float = 100.0
        self.kp: float = 1.2
        self.ki: float = 0.4
        self.kd: float = 0.05
        self.dt: float = 0.05
        
        # Live measurements
        self.current_pv: float = 0.0
        self.control_signal: float = 0.0
        self.error: float = 0.0
        self.rtt_ms: float = 0.0
        
        # Emulation states
        self.active_delay_ms: float = 0.0
        self.active_jitter_ms: float = 0.0
        self.active_loss_pct: float = 0.0
        
        self.lock = threading.Lock()

state = TrialState()

# Initialize the Network Emulation Wrapper (defaulting to eth1 for LXC/private link, or wlan0)
NET_INTERFACE = os.getenv("NET_INTERFACE", "eth1")
tc_wrapper = TrafficControlWrapper(interface=NET_INTERFACE)

# Initialize InfluxDB Telemetry Writer
INFLUX_URL = os.getenv("INFLUXDB_URL", "http://localhost:8086")
INFLUX_TOKEN = os.getenv("INFLUXDB_TOKEN", "testbed_secret_token_123")
INFLUX_ORG = os.getenv("INFLUXDB_ORG", "uconn_meng")
INFLUX_BUCKET = os.getenv("INFLUXDB_BUCKET", "wireless_pid_metrics")
DRY_RUN = os.getenv("INFLUXDB_DRY_RUN", "False").lower() == "true"

telemetry = InfluxWriter(
    url=INFLUX_URL,
    token=INFLUX_TOKEN,
    org=INFLUX_ORG,
    bucket=INFLUX_BUCKET,
    dry_run=DRY_RUN
)
telemetry.start()

# --- Mock Plant & Controller Loop ---
# This is a synthetic simulation thread designed to model a first-order plant 
# and a baseline PID loop, allowing end-to-end telemetry and stress testing 
# validation without physical hardware.
class MockDCSThread(threading.Thread):
    def __init__(self):
        super().__init__(name="MockDCSLoop", daemon=True)
        self._stop_event = threading.Event()
        
    def stop(self):
        self._stop_event.set()
        
    def run(self):
        logger.info("Synthetic DCS Control Loop Thread started.")
        prev_error = 0.0
        integral = 0.0
        
        while not self._stop_event.is_set():
            t_start = time.perf_counter()
            
            with state.lock:
                if not state.is_running:
                    # Thread sleeps briefly when idle to release CPU cycles
                    time.sleep(0.01)
                    continue
                
                # Fetch parameters under lock
                sp = state.setpoint
                kp = state.kp
                ki = state.ki
                kd = state.kd
                dt = state.dt
                pv = state.current_pv
                trial_id = state.trial_id
                alg = state.algorithm
                
            # Execute discrete PID equations: e(t) = r(t) - y(t)
            error = sp - pv
            integral += error * dt
            derivative = (error - prev_error) / dt
            prev_error = error
            
            # Simple saturation protection (anti-windup)
            u_t = (kp * error) + (ki * integral) + (kd * derivative)
            u_t_saturated = max(-100.0, min(100.0, u_t))
            
            # Simulated plant dynamics (First-order process model: dy/dt + a*y = b*u)
            # Modeling thermal/motor time delays and transport lags
            simulated_rtt = state.active_delay_ms / 1000.0  # seconds
            
            # Add stochastic wireless latency representation to feedback loop delay
            time.sleep(max(0.0, simulated_rtt))
            
            # Plant state update
            pv += (u_t_saturated * 0.08) - (pv * 0.015)
            
            # Record metrics to state for C2 display
            with state.lock:
                state.current_pv = pv
                state.control_signal = u_t_saturated
                state.error = error
                state.rtt_ms = state.active_delay_ms  # Simulating measured RTT
            
            # Push high-frequency metrics non-blockingly to InfluxDB
            telemetry.log_control_metrics(
                trial_id=trial_id,
                algorithm=alg,
                setpoint=sp,
                process_variable=pv,
                control_signal=u_t_saturated,
                error=error,
                rtt_ms=state.active_delay_ms
            )
            
            elapsed = time.perf_counter() - t_start
            sleep_time = max(0.0, dt - elapsed)
            time.sleep(sleep_time)
            
        logger.info("Synthetic DCS Control Loop Thread stopped.")

# Start the mock DCS background loop
dcs_thread = MockDCSThread()
dcs_thread.start()


# --- Flask HTTP API Routes ---

@app.route("/api/status", methods=["GET"])
def get_status():
    """Get the live operational status and parameters of the C2 server."""
    with state.lock:
        return jsonify({
            "is_running": state.is_running,
            "trial_id": state.trial_id,
            "algorithm": state.algorithm,
            "setpoint": state.setpoint,
            "kp": state.kp,
            "ki": state.ki,
            "kd": state.kd,
            "dt": state.dt,
            "live_metrics": {
                "process_variable": round(state.current_pv, 3),
                "control_signal": round(state.control_signal, 3),
                "error": round(state.error, 3),
                "rtt_ms": state.rtt_ms
            },
            "network_emulation": {
                "interface": NET_INTERFACE,
                "delay_ms": state.active_delay_ms,
                "jitter_ms": state.active_jitter_ms,
                "loss_pct": state.active_loss_pct
            }
        }), 200

@app.route("/api/start", methods=["POST"])
def start_trial():
    """Start a new control trial run and optionally configure initial conditions."""
    data = request.get_json() or {}
    
    with state.lock:
        if state.is_running:
            return jsonify({"error": "A trial is already actively running. Stop it first."}), 400
        
        state.trial_id = data.get("trial_id", f"trial_{int(time.time())}")
        state.algorithm = data.get("algorithm", "standard_pid")
        state.setpoint = float(data.get("setpoint", 100.0))
        state.kp = float(data.get("kp", state.kp))
        state.ki = float(data.get("ki", state.ki))
        state.kd = float(data.get("kd", state.kd))
        state.current_pv = 0.0 # reset plant state
        state.is_running = True
        
    logger.info(f"C2: Starting Trial '{state.trial_id}' using '{state.algorithm}' algorithm (Setpoint: {state.setpoint})")
    return jsonify({"message": f"Trial {state.trial_id} started successfully.", "state": state.trial_id}), 200

@app.route("/api/stop", methods=["POST"])
def stop_trial():
    """Halt the active trial and reset network rules to a safe baseline."""
    with state.lock:
        if not state.is_running:
            return jsonify({"message": "No active trial to stop. System is already idle."}), 200
        state.is_running = False
        active_id = state.trial_id
        state.trial_id = None
    
    # Tear down any traffic shaping to prevent leaving nodes degraded after tests
    tc_wrapper.clear_rules()
    with state.lock:
        state.active_delay_ms = 0.0
        state.active_jitter_ms = 0.0
        state.active_loss_pct = 0.0
        
    logger.info(f"C2: Trial '{active_id}' stopped by operator request. Traffic control cleared.")
    return jsonify({"message": f"Trial {active_id} stopped and network restored to baseline."}), 200

@app.route("/api/setpoint", methods=["POST"])
def update_setpoint():
    """Handle real-time setpoint adjustments (e.g. React dashboard slider interaction)."""
    data = request.get_json() or {}
    if "setpoint" not in data:
        return jsonify({"error": "Missing 'setpoint' parameter in payload."}), 400
    
    val = float(data["setpoint"])
    with state.lock:
        state.setpoint = val
        
    logger.info(f"C2: Operator adjusted setpoint to {val}")
    return jsonify({"message": f"Setpoint updated to {val}."}), 200

@app.route("/api/parameters", methods=["POST"])
def update_parameters():
    """Adjust PID controller coefficients on-the-fly."""
    data = request.get_json() or {}
    
    with state.lock:
        if "kp" in data: state.kp = float(data["kp"])
        if "ki" in data: state.ki = float(data["ki"])
        if "kd" in data: state.kd = float(data["kd"])
        if "algorithm" in data: state.algorithm = str(data["algorithm"])
        
    logger.info(f"C2: Operational parameters updated: Kp={state.kp}, Ki={state.ki}, Kd={state.kd}, Algorithm={state.algorithm}")
    return jsonify({"message": "Parameters updated successfully."}), 200

@app.route("/api/emulate", methods=["POST"])
def apply_emulation():
    """Apply Linux Traffic Control network shaping profiles to the testing link."""
    data = request.get_json() or {}
    delay_ms = float(data.get("delay_ms", 0.0))
    jitter_ms = float(data.get("jitter_ms", 0.0))
    loss_pct = float(data.get("loss_pct", 0.0))
    
    logger.info(f"C2: Applying network stress: {delay_ms}ms Delay, {jitter_ms}ms Jitter, {loss_pct}% Packet Loss on interface {NET_INTERFACE}.")
    
    success = tc_wrapper.apply_rules(
        delay_ms=delay_ms,
        jitter_ms=jitter_ms,
        loss_pct=loss_pct,
        distribution="normal"
    )
    
    if success:
        with state.lock:
            state.active_delay_ms = delay_ms
            state.active_jitter_ms = jitter_ms
            state.active_loss_pct = loss_pct
        return jsonify({"message": "Network stress profiles successfully applied to kernel."}), 200
    else:
        return jsonify({"error": "Failed to apply network emulation rules. Ensure you are running with CAP_NET_ADMIN privileges."}), 500

@app.route("/api/clear_emulate", methods=["POST"])
def clear_emulation():
    """Manually clear any active network emulation."""
    success = tc_wrapper.clear_rules()
    if success:
        with state.lock:
            state.active_delay_ms = 0.0
            state.active_jitter_ms = 0.0
            state.active_loss_pct = 0.0
        return jsonify({"message": "Network interface restored to line-rate transmission."}), 200
    else:
        return jsonify({"error": "Failed to clear network emulation rules."}), 500


# Application teardown handler
def shutdown_telemetry():
    telemetry.stop()
    dcs_thread.stop()

if __name__ == "__main__":
    # In a production context, deploy using a WSGI/ASGI server like Gunicorn or uWSGI,
    # but for local development and testbed runs, Flask's integrated server on 0.0.0.0 is optimal.
    try:
        app.run(host="0.0.0.0", port=5000, debug=False)
    finally:
        shutdown_telemetry()
