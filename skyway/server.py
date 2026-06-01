#!/usr/bin/env python3
"""Skyway Flask server — REST API + Server-Sent-Events live stream.

Run:  python -m skyway.server   ->   http://localhost:5057

Endpoints
  GET  /                     single-page 4D Cesium app
  GET  /api/airspace         vertiports, skylanes, UASFM grid, airports, region
  GET  /api/faa/advisories   NOTAMs / TFRs (live FAA or simulation)
  GET  /api/operations       all known operations with full 4D trajectories
  GET  /api/traffic          live snapshot of every aircraft in the air now
  POST /api/route/plan       strategic 4D deconfliction for a requested flight
  POST /api/operations/add   inject a new live operation
  POST /api/scenario/reset   reseed the simulation
  GET  /api/stream           SSE: live ticks + newly added operations
  GET  /api/stats            counts + data-share status
"""

from __future__ import annotations

import json
import queue
import time
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

from . import airspace
from .deconfliction import SEP_H, SEP_T, SEP_V
from .traffic import TRAFFIC, VEHICLES, plan_operation

HERE = Path(__file__).resolve().parent
app = Flask(__name__, static_folder=str(HERE / "static"))

# Seed + start the simulation on import so the API is immediately populated.
TRAFFIC.seed_scenario()
TRAFFIC.start()


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/airspace")
def api_airspace():
    return jsonify({
        "region": airspace.REGION,
        "center": airspace.REGION_CENTER,
        "airports": airspace.AIRPORTS,
        "vertiports": airspace.VERTIPORTS,
        "skylanes": airspace.SKYLANES,
        "uasfm_grid": airspace.UASFM_GRID,
        "separation": {"horizontal_m": SEP_H, "vertical_m": SEP_V, "time_s": SEP_T},
    })


@app.route("/api/faa/advisories")
def api_advisories():
    return jsonify({"mode": airspace.FAA.mode, "advisories": airspace.FAA.advisories()})


@app.route("/api/operations")
def api_operations():
    ops = [op.to_dict(include_trajectory=True) for op in TRAFFIC.all_ops()]
    return jsonify({
        "scenario_start": TRAFFIC.scenario_start,
        "server_time": time.time(),
        "count": len(ops),
        "operations": ops,
    })


@app.route("/api/traffic")
def api_traffic():
    return jsonify(TRAFFIC.snapshot())


@app.route("/api/route/plan", methods=["POST"])
def api_route_plan():
    body = request.get_json(force=True, silent=True) or {}
    origin = body.get("origin")
    dest = body.get("dest")
    vehicle = body.get("vehicle", "drone")
    depart_in = float(body.get("depart_in_s", 30))
    reserve = bool(body.get("reserve", True))
    if not origin or not dest:
        return jsonify({"error": "origin and dest are required"}), 400
    op, res = plan_operation(origin, dest, vehicle, time.time() + depart_in, reserve=reserve)
    if not op:
        return jsonify({"ok": False, **res}), 409
    if reserve:
        TRAFFIC.ops.append(op)
        TRAFFIC._emit({"event": "operation_added", "operation": op.to_dict(),
                       "resolution": res})
    return jsonify({"ok": True, "resolution": res, "operation": op.to_dict()})


@app.route("/api/operations/add", methods=["POST"])
def api_add_op():
    body = request.get_json(force=True, silent=True) or {}
    op = TRAFFIC.add_live_operation(body.get("vehicle"))
    if not op:
        return jsonify({"ok": False, "error": "could not place operation"}), 409
    return jsonify({"ok": True, "operation": op.to_dict()})


@app.route("/api/scenario/reset", methods=["POST"])
def api_reset():
    body = request.get_json(force=True, silent=True) or {}
    n = int(body.get("operations", 16))
    placed = TRAFFIC.seed_scenario(n)
    return jsonify({"ok": True, "placed": placed})


@app.route("/api/stats")
def api_stats():
    ops = TRAFFIC.all_ops()
    snap = TRAFFIC.snapshot()
    drones = sum(1 for o in ops if o.vehicle == "drone")
    evtols = sum(1 for o in ops if o.vehicle == "evtol")
    return jsonify({
        "data_share_mode": airspace.FAA.mode,
        "operations_total": len(ops),
        "airborne_now": snap["count"],
        "drones": drones,
        "evtols": evtols,
        "skylanes": len(airspace.SKYLANES),
        "vertiports": len(airspace.VERTIPORTS),
        "advisories": len(airspace.FAA.advisories()),
        "vehicles": VEHICLES,
    })


@app.route("/api/stream")
def api_stream():
    q: "queue.Queue" = queue.Queue(maxsize=100)

    def listener(event):
        try:
            q.put_nowait(event)
        except queue.Full:
            pass

    TRAFFIC.subscribe(listener)

    def gen():
        # Prime the connection with an immediate snapshot.
        yield _sse({"event": "hello", **TRAFFIC.snapshot()})
        try:
            while True:
                try:
                    ev = q.get(timeout=15)
                    yield _sse(ev)
                except queue.Empty:
                    yield _sse({"event": "tick", **TRAFFIC.snapshot()})
        finally:
            TRAFFIC.unsubscribe(listener)

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _sse(obj) -> str:
    return f"data: {json.dumps(obj)}\n\n"


def main():
    import os
    port = int(os.environ.get("SKYWAY_PORT", "5057"))
    print(f"Skyway UTM running -> http://localhost:{port}  (data share: {airspace.FAA.mode})")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
