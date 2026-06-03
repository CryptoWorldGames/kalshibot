#!/usr/bin/env python3
"""
All-Night Bot Monitor
Checks bot health with adaptive frequency:
- Every 5 minutes for first 1 hour
- Every 10 minutes for next 1 hour
- Every 20 minutes after that (indefinitely)

Run: python monitor_all_night.py
"""

import time
import requests
import json
from datetime import datetime
from pathlib import Path

PORTS = [5000, 5003]  # Kalshi Bot 1 and Bot 2
CHECK_TIMEOUT = 5
STAGE_1_MINS = 60    # 5-min checks for 1 hour
STAGE_2_MINS = 60    # 10-min checks for 1 hour
# After that: 20-min checks forever

def check_bot(port):
    """Quick health check - just ping the status endpoint"""
    try:
        response = requests.get(
            f"http://localhost:{port}/api/manager/status",
            timeout=CHECK_TIMEOUT
        )
        return response.status_code == 200 and "running" in response.text.lower()
    except Exception as e:
        return False

def log(msg):
    """Print with timestamp"""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")

def main():
    log("=" * 70)
    log("ALL-NIGHT BOT MONITOR STARTED")
    log("=" * 70)
    log(f"Monitoring ports: {PORTS}")
    log(f"Stage 1: Every 5 min for 1 hour")
    log(f"Stage 2: Every 10 min for 1 hour")
    log(f"Stage 3: Every 20 min indefinitely")
    log("=" * 70)

    start_time = time.time()
    check_count = 0
    failed_count = 0

    while True:
        elapsed_secs = time.time() - start_time
        elapsed_mins = elapsed_secs / 60

        # Determine check interval based on stage
        if elapsed_mins < STAGE_1_MINS:
            stage = 1
            interval = 300  # 5 minutes
        elif elapsed_mins < (STAGE_1_MINS + STAGE_2_MINS):
            stage = 2
            interval = 600  # 10 minutes
        else:
            stage = 3
            interval = 1200  # 20 minutes

        # Perform checks
        check_count += 1
        all_ok = True
        status_line = ""

        for port in PORTS:
            bot_name = f"Bot{port}" if port == 5000 else f"Bot2-{port}"
            is_ok = check_bot(port)
            status = "✓" if is_ok else "✗"
            status_line += f" {bot_name}:{status}"
            if not is_ok:
                all_ok = False
                failed_count += 1

        # Log status
        hours = int(elapsed_mins / 60)
        mins = int(elapsed_mins % 60)
        log(f"[S{stage}] Check #{check_count} ({hours}h{mins}m elapsed){status_line}")

        if not all_ok:
            log(f"⚠️  ALERT: Some bots offline! Failed checks: {failed_count}")

        # Sleep until next check
        time.sleep(interval)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("\nMonitoring stopped by user")
    except Exception as e:
        log(f"ERROR: {e}")
