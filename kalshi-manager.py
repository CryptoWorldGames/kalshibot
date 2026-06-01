#!/usr/bin/env python3
"""
KalshiBot Process Manager
Manages Flask subprocess with remote control (start/stop/restart)
Run: python manager.py
"""

import subprocess
import signal
import socket
import sys
import time
import json
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
import threading

HERE = Path(__file__).resolve().parent
FLASK_PROCESS = None
FLASK_PORT = 5000
MANAGER_PORT = 5100  # Manager runs on Flask port + 100
RUNNING = True
INTENTIONALLY_STOPPED = False  # Track if user manually stopped Flask

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
    """Start Flask process"""
    global FLASK_PROCESS, INTENTIONALLY_STOPPED
    if FLASK_PROCESS is not None and check_flask_alive():
        print("[manager] Flask already running")
        return

    INTENTIONALLY_STOPPED = False  # Clear the stop flag when user starts it
    print("[manager] Starting Flask...")
    FLASK_PROCESS = subprocess.Popen(
        [sys.executable, "app.py"],
        cwd=HERE,
        creationflags=subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0
    )
    print(f"[manager] Flask started (PID: {FLASK_PROCESS.pid})")

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

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Bind manager HTTP server — retry if port is briefly still in use from prior run
    manager_server = None
    for attempt in range(10):
        try:
            manager_server = HTTPServer(("0.0.0.0", MANAGER_PORT), ManagerHandler)
            manager_server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            break
        except OSError as e:
            print(f"[manager] Port {MANAGER_PORT} busy (attempt {attempt+1}/10), retrying in 2s... ({e})")
            time.sleep(2)

    if manager_server is None:
        print(f"[manager] ERROR: Could not bind port {MANAGER_PORT} after 10 attempts. Continuing without remote control.")
    else:
        print(f"[manager] Manager running on port {MANAGER_PORT}")
        print(f"[manager] Flask running on port {FLASK_PORT}")
        print("[manager] Endpoints: /api/manager/status, /start, /stop, /restart")
        try:
            manager_server.serve_forever()
        except KeyboardInterrupt:
            signal_handler(None, None)
