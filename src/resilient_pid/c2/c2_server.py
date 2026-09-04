#!/usr/bin/env python3
"""
src/resilient_pid/c2/c2_server.py
Lightweight Command & Control (C2) state engine.
Runs as a headless REST API by default, with an optional minimal web console.
"""
import os
import sys
import time
import socket
import logging
import argparse
from flask import Flask, jsonify, request, render_template_string

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("C2Server")

app = Flask(__name__)

# Shared runtime state
system_state = {
    "is_running": False,
    "trial_id": f"trial_{int(time.time())}",
    "algorithm": "baseline",      # baseline | smith | resilient
    "setpoint": 50.0,             # Process Variable target (0-100%)
    "active_profile": "nominal",
    "updated_at": time.time()
}

UI_ENABLED = False

MINIMAL_HTML = """
<!DOCTYPE html>
<html>
<head><title>DCS C2 Console (Minimal)</title></head>
<body style="font-family: monospace; padding: 2rem; background: #222; color: #eee;">
  <h2>DCS C2 Test Console</h2>
  <p>Status: <span id="status">Polling...</span></p>
  <button onclick="send(true)">Start Loop</button>
  <button onclick="send(false)">Stop Loop</button>
  <input type="number" id="sp" value="50" style="width: 60px;">
  <button onclick="updateSP()">Set SP</button>
  <script>
    async function poll() {
      const res = await fetch('/api/status');
      const d = await res.json();
      document.getElementById('status').innerText = JSON.stringify(d);
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
    if UI_ENABLED:
        return render_template_string(MINIMAL_HTML)
    return jsonify({
        "service": "resilient-wireless-pid-c2",
        "status": "online",
        "mode": "headless-api",
        "api_endpoints": ["/api/status", "/api/control", "/api/start", "/api/stop"]
    }), 200

@app.route("/api/status", methods=["GET"])
def get_status():
    return jsonify(system_state), 200

@app.route("/api/control", methods=["POST"])
def update_control():
    payload = request.get_json(force=True)
    for key in ["is_running", "setpoint", "algorithm", "trial_id", "active_profile"]:
        if key in payload:
            system_state[key] = payload[key]
    system_state["updated_at"] = time.time()
    logger.info("C2 State: Running=%s, Alg=%s, SP=%.2f, Trial=%s",
                system_state["is_running"], system_state["algorithm"],
                system_state["setpoint"], system_state["trial_id"])
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
    parser.add_argument("--enable-ui", action="store_true", help="Mount minimal operator web UI at /")
    return parser.parse_args()

def main():
    global UI_ENABLED
    args = parse_args()
    UI_ENABLED = args.enable_ui
    logger.info("C2 Server active on http://%s:%d (UI: %s)", args.host, args.port, "ENABLED" if UI_ENABLED else "HEADLESS")
    app.run(host=args.host, port=args.port, debug=False)

if __name__ == "__main__":
    main()