#!/usr/bin/env python3
"""
src/resilient_pid/c2/c2_server.py
Lightweight Command & Control (C2) state engine.
Runs as a headless REST API with dynamically toggled web UI support.
"""

import argparse
import json
import logging
import os
import socket
import time
from typing import TypedDict

from flask import Flask, jsonify, render_template_string, request

# Suppress noisy HTTP access logs from Werkzeug
logging.getLogger("werkzeug").setLevel(logging.ERROR)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("C2Server")

app = Flask(__name__)


class SystemState(TypedDict):
    """Schema definition for the C2 runtime shared state."""

    is_running: bool
    trial_id: str
    algorithm: str
    setpoint: float
    active_profile: str
    ui_enabled: bool
    updated_at: float


# Shared runtime state
system_state: SystemState = {
    "is_running": False,
    "trial_id": f"trial_{int(time.time())}",
    "algorithm": "discrete",  # discrete | smith | resilient
    "setpoint": 0.0,  # Setpoint initialized to 0.0 for safety
    "active_profile": "nominal",
    "ui_enabled": False,  # Dynamic UI toggle
    "updated_at": time.time(),
}

# Network defaults for direct hardware shutdown fallback
PLANT_HOST = os.getenv("PLANT_IP", "127.0.0.1")
PLANT_PORT = int(os.getenv("PLANT_PORT", "5005"))


def dispatch_zero_actuation_to_plant() -> bool:
    """
    Sends explicit zero-power UDP datagrams to the plant daemon to guarantee
    the fan does not remain latched at its previous duty cycle.
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.2)
        pkt = json.dumps({"seq": 0, "u": 0.0, "t_send": time.perf_counter()})
        payload = pkt.encode("utf-8")
        # Send redundant datagrams to guard against UDP packet loss
        for _ in range(3):
            sock.sendto(payload, (PLANT_HOST, PLANT_PORT))
            time.sleep(0.005)
        sock.close()
        logger.info(
            "Dispatched zero-actuation shutdown datagram (u=0.0%%) to plant at %s:%d",
            PLANT_HOST,
            PLANT_PORT,
        )
        return True
    except Exception as err:
        logger.warning("Could not dispatch direct plant shutdown frame: %s", err)
        return False


MINIMAL_HTML = """
<!DOCTYPE html>
<html>
<head><title>DCS C2 Console (Minimal)</title></head>
<body style="font-family: monospace; padding: 2rem; background: #1a1a1a; color: #eee;">
  <h2>DCS C2 Diagnostic Console</h2>
  <p id="status">Status: Connecting...</p>
  <button onclick="send(true)" style="background: #2e7d32; color: white; padding: 0.5rem 1rem;">Start Loop</button>
  <button onclick="stopActuation()" style="background: #c62828; color: white; padding: 0.5rem 1rem;">Stop Loop</button>
  <br><br>
  <input type="number" id="sp" value="60" style="width: 60px; padding: 0.3rem;">
  <button onclick="updateSP()">Set SP</button>

  <script>
    async function poll() {
      try {
        const res = await fetch('/api/status');
        const d = await res.json();
        document.getElementById('status').innerText = JSON.stringify(d, null, 2);
      } catch (err) {
        document.getElementById('status').innerText = "Offline";
      }
    }
    async function send(run) {
      await fetch('/api/control', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({is_running: run})
      });
      poll();
    }
    async function stopActuation() {
      await fetch('/api/stop', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'}
      });
      poll();
    }
    async function updateSP() {
      const val = parseFloat(document.getElementById('sp').value);
      await fetch('/api/control', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({setpoint: val, is_running: true})
      });
      poll();
    }
    setInterval(poll, 1000);
    poll();
  </script>
</body>
</html>
"""


@app.route("/", methods=["GET"])
def index():
    if system_state["ui_enabled"]:
        return render_template_string(MINIMAL_HTML)
    return jsonify(
        {
            "service": "resilient-wireless-pid-c2",
            "status": "online",
            "mode": "headless-api",
            "ui_enabled": False,
            "api_endpoints": ["/api/status", "/api/control", "/api/start", "/api/stop"],
        }
    ), 200


@app.route("/api/status", methods=["GET"])
def get_status():
    return jsonify(system_state), 200


@app.route("/api/control", methods=["POST"])
def update_control():
    payload = request.get_json(force=True)
    if "is_running" in payload:
        system_state["is_running"] = bool(payload["is_running"])
    if "setpoint" in payload:
        system_state["setpoint"] = float(payload["setpoint"])
    if "algorithm" in payload:
        system_state["algorithm"] = str(payload["algorithm"])
    if "trial_id" in payload:
        system_state["trial_id"] = str(payload["trial_id"])
    if "active_profile" in payload:
        system_state["active_profile"] = str(payload["active_profile"])
    if "ui_enabled" in payload:
        system_state["ui_enabled"] = bool(payload["ui_enabled"])

    # If control specifically requests running=False, zero setpoint
    if payload.get("is_running") is False:
        system_state["setpoint"] = 0.0
        dispatch_zero_actuation_to_plant()

    system_state["updated_at"] = time.time()
    logger.info(
        "C2 State: Running=%s, Alg=%s, SP=%.2f, UI=%s, Trial=%s",
        system_state["is_running"],
        system_state["algorithm"],
        system_state["setpoint"],
        system_state["ui_enabled"],
        system_state["trial_id"],
    )
    return jsonify({"status": "ok", "state": system_state}), 200


@app.route("/api/start", methods=["POST"])
def start_trial():
    payload = request.get_json(silent=True) or {}
    system_state["is_running"] = True
    system_state["trial_id"] = str(
        payload.get("trial_id", f"{system_state['algorithm']}_{int(time.time())}")
    )
    if "setpoint" in payload:
        system_state["setpoint"] = float(payload["setpoint"])
    elif float(system_state["setpoint"]) <= 0.0:
        system_state["setpoint"] = 60.0  # Default to aerodynamic sweet spot

    system_state["updated_at"] = time.time()
    logger.info(
        "Trial started: %s (SP: %.1f%%)",
        system_state["trial_id"],
        system_state["setpoint"],
    )
    return jsonify(
        {
            "status": "started",
            "trial_id": system_state["trial_id"],
            "setpoint": system_state["setpoint"],
        }
    ), 200


@app.route("/api/stop", methods=["POST"])
def stop_trial():
    """
    Safely halts active control loop, zeroes setpoint, and forces actuator de-energization.
    """
    system_state["is_running"] = False
    system_state["setpoint"] = 0.0
    system_state["updated_at"] = time.time()

    # Actively command zero duty to hardware
    dispatch_zero_actuation_to_plant()

    logger.info("C2 Stop invoked: Controller loop disengaged and fan de-energized.")
    return jsonify({"status": "stopped", "actuator": 0.0, "setpoint": 0.0}), 200


def parse_args():
    parser = argparse.ArgumentParser(description="Resilient Wireless DCS C2 Server")
    parser.add_argument(
        "--host", default="0.0.0.0", help="HTTP bind address (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("C2_PORT", "5050")),
        help="Port (default: 5050)",
    )
    parser.add_argument(
        "--enable-ui", action="store_true", help="Enable HTML UI at / on boot"
    )
    return parser.parse_args()


def main():
    args = parse_args()
    system_state["ui_enabled"] = args.enable_ui
    logger.info(
        "C2 Server active on http://%s:%d (UI: %s) | Plant Target: %s:%d",
        args.host,
        args.port,
        "ENABLED" if args.enable_ui else "HEADLESS",
        PLANT_HOST,
        PLANT_PORT,
    )
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
