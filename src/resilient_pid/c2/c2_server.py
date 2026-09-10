#!/usr/bin/env python3
"""
src/resilient_pid/c2/c2_server.py
Lightweight Command & Control (C2) state engine.
Runs as a headless REST API with dynamically toggled web UI support.
"""
import os
import sys
import time
import logging
import argparse
from flask import Flask, jsonify, request, render_template_string

# Suppress noisy HTTP access logs from Werkzeug
logging.getLogger("werkzeug").setLevel(logging.ERROR)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("C2Server")

app = Flask(__name__)

# Shared runtime state
system_state = {
    "is_running": False,
    "trial_id": f"trial_{int(time.time())}",
    "algorithm": "baseline",      # baseline | smith | resilient
    "setpoint": 50.0,             # Target process variable (0-100%)
    "active_profile": "nominal",
    "ui_enabled": False,          # Dynamic UI toggle
    "updated_at": time.time()
}

MINIMAL_HTML = """
<!DOCTYPE html>
<html>
<head><title>DCS C2 Console (Minimal)</title></head>
<body style="font-family: monospace; padding: 2rem; background: #1a1a1a; color: #eee;">
  <h2>DCS C2 Diagnostic Console</h2>
  <p id="status">Status: Connecting...</p>
  <button onclick="send(true)">Start Loop</button>
  <button onclick="send(false)">Stop Loop</button>
  <input type="number" id="sp" value="50" style="width: 60px;">
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
    async function updateSP() {
      const val = parseFloat(document.getElementById('sp').value);
      await fetch('/api/control', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({setpoint: val})
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
    return jsonify({
        "service": "resilient-wireless-pid-c2",
        "status": "online",
        "mode": "headless-api",
        "ui_enabled": False,
        "api_endpoints": ["/api/status", "/api/control", "/api/start", "/api/stop"]
    }), 200

@app.route("/api/status", methods=["GET"])
def get_status():
    return jsonify(system_state), 200

@app.route("/api/control", methods=["POST"])
def update_control():
    payload = request.get_json(force=True)
    for key in ["is_running", "setpoint", "algorithm", "trial_id", "active_profile", "ui_enabled"]:
        if key in payload:
            system_state[key] = payload[key]
    system_state["updated_at"] = time.time()
    logger.info("C2 State: Running=%s, Alg=%s, SP=%.2f, UI=%s, Trial=%s",
                system_state["is_running"], system_state["algorithm"],
                system_state["setpoint"], system_state["ui_enabled"], system_state["trial_id"])
    return jsonify({"status": "ok", "state": system_state}), 200

@app.route("/api/start", methods=["POST"])
def start_trial():
    payload = request.get_json(silent=True) or {}
    system_state["is_running"] = True
    system_state["trial_id"] = payload.get("trial_id", f"{system_state['algorithm']}_{int(time.time())}")
    system_state["updated_at"] = time.time()
    return jsonify({"status": "started", "trial_id": system_state["trial_id"]}), 200

@app.route("/api/stop", methods=["POST"])
def stop_trial():
    system_state["is_running"] = False
    system_state["updated_at"] = time.time()
    return jsonify({"status": "stopped"}), 200

def parse_args():
    parser = argparse.ArgumentParser(description="Resilient Wireless DCS C2 Server")
    parser.add_argument("--host", default="0.0.0.0", help="HTTP bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=int(os.getenv("C2_PORT", "5000")), help="Port (default: 5000)")
    parser.add_argument("--enable-ui", action="store_true", help="Enable HTML UI at / on boot")
    return parser.parse_args()

def main():
    args = parse_args()
    system_state["ui_enabled"] = args.enable_ui
    logger.info("C2 Server active on http://%s:%d (UI: %s)",
                args.host, args.port, "ENABLED" if args.enable_ui else "HEADLESS")
    app.run(host=args.host, port=args.port, debug=False)

if __name__ == "__main__":
    main()