#!/usr/bin/env python3
"""
KalshiBot Process Manager
Manages Flask subprocess with remote control (start/stop/restart)
Run: python KalshiBot_manager.py
Ports configured via .env file (KALSHI_BOT_PORT, KALSHI_MANAGER_PORT)
"""

import subprocess
import signal
import socket
import sys
import time
import json
import os
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
import threading

# Load environment variables from .env
HERE = Path(__file__).resolve().parent
env_file = HERE / ".env"
if env_file.exists():
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                key, value = line.split("=", 1)
                os.environ[key.strip()] = value.strip()

FLASK_PORT = int(os.getenv("KALSHI_BOT_PORT", "5003"))
MANAGER_PORT = int(os.getenv("KALSHI_MANAGER_PORT", "5103"))
FLASK_PROCESS = None
RUNNING = True
INTENTIONALLY_STOPPED = False

class ReusingHTTPServer(HTTPServer):
    allow_reuse_address = True

class ManagerHandler(BaseHTTPRequestHandler):
    """HTTP handler for remote control commands"""

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/api/manager/status":
            is_alive = check_flask_alive()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"running": is_alive, "port": FLASK_PORT}).encode())
        else:
            self.send_response(404)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/manager/stop":
            stop_flask()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "stopped"}).encode())

        elif path == "/api/manager/start":
            start_flask()
            time.sleep(2)  # Wait for Flask to start
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "started"}).encode())

        elif path == "/api/manager/restart":
            stop_flask()
            time.sleep(1)
            start_flask()
            time.sleep(2)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "restarted"}).encode())

        elif path == "/api/manager/update":
            # git pull + restart Flask — lets the user deploy new code from any device
            # (phone/laptop) with zero desktop access. Returns the git output so the UI
            # can show what changed (or why it failed).
            result = {"status": "updated"}
            try:
                out = subprocess.run(["git", "pull"], cwd=str(HERE),
                                     capture_output=True, text=True, timeout=90)
                result["git"] = (out.stdout + out.stderr).strip()[-3000:]
                result["git_ok"] = (out.returncode == 0)
            except Exception as e:
                result["git_error"] = str(e)
                result["git_ok"] = False
            stop_flask()
            time.sleep(1)
            start_flask()
            time.sleep(2)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())

        else:
            self.send_response(404)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

    def log_message(self, format, *args):
        """Suppress request logging"""
        pass

def check_flask_alive():
    """Check if Flask process is running"""
    global FLASK_PROCESS
    if FLASK_PROCESS is None:
        return False
    return FLASK_PROCESS.poll() is None

def start_flask():
    """Start Flask process with full path and distinct process title"""
    global FLASK_PROCESS, INTENTIONALLY_STOPPED
    if FLASK_PROCESS is not None and check_flask_alive():
        print("[manager] Flask already running")
        return

    INTENTIONALLY_STOPPED = False
    app_path = HERE / "app.py"
    print(f"[manager] Starting Flask from {app_path}...")

    # Build command with full path and distinct process title for Windows Task Manager
    cmd = [sys.executable, str(app_path)]

    FLASK_PROCESS = subprocess.Popen(
        cmd,
        cwd=str(HERE),
        creationflags=subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0
    )
    print(f"[manager] Flask started on port {FLASK_PORT} (PID: {FLASK_PROCESS.pid})")

def stop_flask():
    """Stop Flask process gracefully"""
    global FLASK_PROCESS, INTENTIONALLY_STOPPED
    if FLASK_PROCESS is None or not check_flask_alive():
        print("[manager] Flask not running")
        return

    INTENTIONALLY_STOPPED = True  # Mark that user stopped it
    print("[manager] Stopping Flask...")
    try:
        FLASK_PROCESS.terminate()
        FLASK_PROCESS.wait(timeout=5)
    except subprocess.TimeoutExpired:
        print("[manager] Forcing Flask shutdown...")
        FLASK_PROCESS.kill()
    print("[manager] Flask stopped")

def auto_restart_monitor():
    """Monitor Flask and auto-restart if it crashes (only if not intentionally stopped)"""
    while RUNNING:
        time.sleep(5)  # Check every 5 seconds
        if FLASK_PROCESS is not None and not check_flask_alive() and not INTENTIONALLY_STOPPED:
            print("[manager] Flask crashed! Auto-restarting...")
            start_flask()

AUTO_UPDATE_INTERVAL = 300  # 5 minutes — how often to auto-pull new code from GitHub

def auto_update_monitor():
    """Every AUTO_UPDATE_INTERVAL seconds, run `git pull`. If new code arrived,
    restart Flask so it loads. This is FREE — just git running locally, no API/AI
    calls. Lets the user deploy from any device by pushing to GitHub; the desktop
    picks the update up on its own with zero clicks."""
    first_pull = True
    while RUNNING:
        # On startup, pull immediately. After that, wait AUTO_UPDATE_INTERVAL between pulls.
        if not first_pull:
            for _ in range(AUTO_UPDATE_INTERVAL):
                if not RUNNING:
                    return
                time.sleep(1)
        first_pull = False

        try:
            out = subprocess.run(["git", "pull"], cwd=str(HERE),
                                 capture_output=True, text=True, timeout=90)
            combined = (out.stdout + out.stderr).strip()
            up_to_date = ("Already up to date" in combined or
                          "Already up-to-date" in combined)
            if out.returncode == 0 and not up_to_date:
                print(f"[auto-update] New code pulled — restarting Flask:\n{combined[-500:]}")
                stop_flask()
                time.sleep(1)
                start_flask()
                time.sleep(2)
            elif out.returncode == 0:
                print("[auto-update] Checked GitHub — already up to date.")
            else:
                print(f"[auto-update] git pull failed: {combined[-300:]}")
        except Exception as e:
            print(f"[auto-update] error: {e}")

def signal_handler(sig, frame):
    """Handle shutdown signals"""
    global RUNNING
    print("\n[manager] Shutting down...")
    RUNNING = False
    stop_flask()
    sys.exit(0)

if __name__ == "__main__":
    start_flask()

    monitor_thread = threading.Thread(target=auto_restart_monitor, daemon=True)
    monitor_thread.start()

    # Auto-pull new code from GitHub every 15 min so deploys go live with zero clicks
    update_thread = threading.Thread(target=auto_update_monitor, daemon=True)
    update_thread.start()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Bind manager HTTP server — ReusingHTTPServer sets SO_REUSEADDR before bind
    manager_server = None
    for attempt in range(15):
        try:
            manager_server = ReusingHTTPServer(("0.0.0.0", MANAGER_PORT), ManagerHandler)
            break
        except OSError as e:
            print(f"[manager] Port {MANAGER_PORT} busy (attempt {attempt+1}/15), retrying in 2s... ({e})")
            time.sleep(2)

    if manager_server is None:
        print(f"[manager] ERROR: Could not bind port {MANAGER_PORT}. Flask still running — no remote control.")
        # Keep process alive so Flask auto-restart still works
        while RUNNING:
            time.sleep(5)
    else:
        print(f"[manager] Manager running on port {MANAGER_PORT}")
        print(f"[manager] Flask running on port {FLASK_PORT}")
        print("[manager] Endpoints: /api/manager/status, /start, /stop, /restart")
        try:
            manager_server.serve_forever()
        except Exception as e:
            print(f"[manager] Server error: {e}")
            signal_handler(None, None)
