"""Operations, route planning, 4D trajectory generation, and live traffic.

An *operation* is one drone or eVTOL flight: a route through the skylane
network, turned into a 4D trajectory (climb -> cruise -> descend), strategically
deconflicted, and reserved. Each active operation broadcasts Remote ID
telemetry (FAA Part 89). A handful of non-cooperative crewed aircraft are also
modelled as ADS-B-style "see and avoid" traffic.
"""

from __future__ import annotations

import heapq
import itertools
import random
import threading
import time
import uuid
from typing import Callable, List, Optional

from . import airspace, geo
from .deconfliction import ENGINE, SEP_H, SEP_V

FT = geo.FT

# Vehicle performance profiles.
VEHICLES = {
    "drone": {"label": "Delivery drone", "cruise_mps": 18.0, "climb_mps": 4.0,
              "color": "#36d399", "size": 1.0, "alt_offset_m": 0},
    "evtol": {"label": "eVTOL air taxi", "cruise_mps": 55.0, "climb_mps": 6.0,
              "color": "#38bdf8", "size": 1.8, "alt_offset_m": 15},
}

OPERATORS = ["SkyDrop", "AeroCab", "MetroLift", "ZiplineTX", "Joby-DFW", "WingX", "VertiGo"]

_callsign_counter = itertools.count(1)


def _build_graph():
    graph = {}
    for lane in airspace.SKYLANES:
        graph.setdefault(lane["from"], []).append(lane)
    return graph


GRAPH = _build_graph()


def plan_route(origin: str, dest: str, avoid_prohibited: bool = True) -> Optional[List[str]]:
    """Shortest node path origin->dest over the directed skylane graph.

    When `avoid_prohibited` is set, corridors whose tube intersects an active
    prohibited advisory (TFR) are dropped first so the route steers around the
    no-fly area. If that disconnects origin from dest, the avoidance is relaxed
    so the planner can still return a path (which deconfliction may then reject).
    """
    if origin == dest:
        return None

    def search(avoid):
        dist = {origin: 0.0}
        prev = {}
        pq = [(0.0, origin)]
        while pq:
            d, node = heapq.heappop(pq)
            if node == dest:
                break
            if d > dist.get(node, float("inf")):
                continue
            for lane in GRAPH.get(node, []):
                if avoid and airspace.lane_crosses_prohibited(lane):
                    continue
                nd = d + lane["length_km"]
                if nd < dist.get(lane["to"], float("inf")):
                    dist[lane["to"]] = nd
                    prev[lane["to"]] = node
                    heapq.heappush(pq, (nd, lane["to"]))
        if dest not in prev:
            return None
        path = [dest]
        while path[-1] != origin:
            path.append(prev[path[-1]])
        return list(reversed(path))

    return (avoid_prohibited and search(True)) or search(False)


def _route_points(node_ids: List[str]):
    return [[airspace.VP[n]["lat"], airspace.VP[n]["lon"]] for n in node_ids]


def _cruise_alt(node_ids: List[str], vehicle: str, alt_bump: float) -> float:
    """Cruise altitude (m AGL) from the lanes traversed, capped by UASFM."""
    bands = []
    for a, b in zip(node_ids, node_ids[1:]):
        for lane in GRAPH.get(a, []):
            if lane["to"] == b:
                bands.append((lane["floor_m"] + lane["ceil_m"]) / 2)
    base = (sum(bands) / len(bands)) if bands else 90.0
    alt = base + VEHICLES[vehicle]["alt_offset_m"] + alt_bump
    # Respect the most restrictive UASFM ceiling along the route.
    ceil = min(airspace.uasfm_ceiling_m(p[0], p[1]) for p in _route_points(node_ids))
    return max(20.0, min(alt, max(20.0, ceil - 5)))


def build_trajectory(node_ids: List[str], vehicle: str, t_start: float, alt_bump: float = 0.0):
    """Return (display_samples, reserve_samples, summary).

    display_samples: dicts {t,lat,lon,alt,hdg,spd} for visualisation.
    reserve_samples: (lat,lon,alt,t) tuples for the deconfliction engine.
    """
    prof = VEHICLES[vehicle]
    pts = _route_points(node_ids)
    ground = geo.polyline_length(pts)
    cruise_alt = _cruise_alt(node_ids, vehicle, alt_bump)
    elev0 = airspace.VP[node_ids[0]]["elev_m"]
    elev1 = airspace.VP[node_ids[-1]]["elev_m"]
    spd = prof["cruise_mps"]

    # Continuous motion with a trapezoidal altitude profile: the aircraft moves
    # forward the whole time (no vertical hover columns) so a cockpit camera
    # always has a valid forward vector. Climb/descend over the route ends.
    climb_d = min(0.28 * ground, cruise_alt * 6)
    desc_d = min(0.28 * ground, cruise_alt * 6)
    if climb_d + desc_d > ground:
        climb_d = desc_d = ground * 0.4
    total_t = max(ground / spd, 1.0)

    dt = 3.0
    disp, res = [], []
    t = 0.0
    while t <= total_t + 1e-6:
        dist = min(ground, spd * t)
        frac = dist / ground if ground else 1.0
        if climb_d > 0 and dist < climb_d:
            agl = cruise_alt * (dist / climb_d)
        elif desc_d > 0 and dist > ground - desc_d:
            agl = cruise_alt * max(0.0, (ground - dist) / desc_d)
        else:
            agl = cruise_alt
        base = elev0 + (elev1 - elev0) * frac
        alt = base + agl
        lat, lon, hdg = geo.interpolate_along(pts, frac)
        ts = t_start + t
        disp.append({"t": round(ts, 1), "lat": round(lat, 6), "lon": round(lon, 6),
                     "alt": round(alt, 1), "hdg": round(hdg, 1), "spd": round(spd, 1)})
        res.append((lat, lon, alt, ts))
        t += dt
    summary = {
        "ground_km": round(ground / 1000, 2),
        "cruise_alt_m": round(cruise_alt, 1),
        "cruise_alt_ft": round(cruise_alt / FT),
        "duration_s": round(total_t, 1),
        "t_start": round(t_start, 1),
        "t_end": round(t_start + total_t, 1),
    }
    return disp, res, summary


class Operation:
    def __init__(self, vehicle, node_ids, t_start, operator=None, alt_bump=0.0,
                 source="UTM", callsign=None):
        self.id = "OP-" + uuid.uuid4().hex[:8].upper()
        self.vehicle = vehicle
        self.operator = operator or random.choice(OPERATORS)
        self.node_ids = node_ids
        self.source = source            # UTM (deconflicted) or ADS-B (advisory)
        n = next(_callsign_counter)
        self.callsign = callsign or f"{'AC' if vehicle=='evtol' else 'DR'}{1000 + n}"
        self.display, self.reserve, self.summary = build_trajectory(
            node_ids, vehicle, t_start, alt_bump)
        self.rid = {                    # FAA Part 89 Remote ID message set
            "uas_id": "FA3" + uuid.uuid4().hex[:9].upper(),
            "ua_type": "rotorcraft" if vehicle == "evtol" else "multirotor",
            "operator_id": self.operator,
            "control_station": {"lat": airspace.VP[node_ids[0]]["lat"],
                                "lon": airspace.VP[node_ids[0]]["lon"]},
            "emergency": False,
        }

    # ---- time helpers -------------------------------------------------
    @property
    def t_start(self):
        return self.summary["t_start"]

    @property
    def t_end(self):
        return self.summary["t_end"]

    def state_at(self, now: float):
        """Interpolated live state, or None if not airborne at `now`."""
        if now < self.t_start or now > self.t_end:
            return None
        s = self.display
        # binary-ish linear scan (trajectories are short)
        for a, b in zip(s, s[1:]):
            if a["t"] <= now <= b["t"]:
                f = (now - a["t"]) / ((b["t"] - a["t"]) or 1)
                return {
                    "lat": a["lat"] + (b["lat"] - a["lat"]) * f,
                    "lon": a["lon"] + (b["lon"] - a["lon"]) * f,
                    "alt": a["alt"] + (b["alt"] - a["alt"]) * f,
                    "hdg": a["hdg"], "spd": a["spd"],
                }
        return s[-1]

    def to_dict(self, include_trajectory=True):
        d = {
            "id": self.id, "callsign": self.callsign, "vehicle": self.vehicle,
            "operator": self.operator, "source": self.source,
            "route": self.node_ids, "summary": self.summary,
            "rid": self.rid, "color": VEHICLES[self.vehicle]["color"],
        }
        if include_trajectory:
            d["trajectory"] = self.display
        return d


def plan_operation(origin: str, dest: str, vehicle: str, t_request: float,
                   reserve: bool = True):
    """Strategically deconflict and (optionally) reserve a new operation.

    Returns (operation, resolution_dict) or (None, error_dict).
    """
    if vehicle not in VEHICLES:
        return None, {"error": f"unknown vehicle '{vehicle}'"}
    route = plan_route(origin, dest)
    if not route:
        return None, {"error": f"no skylane route {origin}->{dest}"}

    attempts = []
    # Try the requested slot, then time-shifts, then altitude bumps.
    for alt_bump in (0.0, 30.0, -30.0, 60.0):
        for dshift in range(0, 13):
            t0 = t_request + dshift * 30
            op = Operation(vehicle, route, t0, alt_bump=alt_bump)
            conflict = ENGINE.check(op.reserve, ignore_op=op.id)
            if conflict is None:
                if reserve:
                    ENGINE.reserve(op.id, op.reserve)
                resolution = {
                    "status": "cleared",
                    "delay_s": dshift * 30,
                    "alt_bump_m": alt_bump,
                    "attempts": attempts,
                    "route": route,
                }
                return op, resolution
            attempts.append({"delay_s": dshift * 30, "alt_bump_m": alt_bump,
                             "blocked_by": conflict.get("reason"),
                             "with": conflict.get("with")})
    return None, {"error": "no conflict-free 4D slot found", "attempts": attempts}


# ---------------------------------------------------------------------------
# Live traffic state + simulator
# ---------------------------------------------------------------------------
class TrafficManager:
    def __init__(self, seed: int = 7):
        self.rng = random.Random(seed)
        self.ops: List[Operation] = []
        self.external: List[Operation] = []
        self._lock = threading.RLock()
        self._listeners: List[Callable] = []
        self.scenario_start = time.time()
        self._running = False
        self._thread = None

    # ---- pub/sub for SSE ---------------------------------------------
    def subscribe(self, cb: Callable):
        with self._lock:
            self._listeners.append(cb)

    def unsubscribe(self, cb: Callable):
        with self._lock:
            if cb in self._listeners:
                self._listeners.remove(cb)

    def _emit(self, event: dict):
        for cb in list(self._listeners):
            try:
                cb(event)
            except Exception:
                pass

    # ---- scenario seeding --------------------------------------------
    def seed_scenario(self, n_ops: int = 16):
        ENGINE.reset()
        with self._lock:
            self.ops.clear()
            self.external.clear()
        now = time.time()
        self.scenario_start = now
        node_ids = [v["id"] for v in airspace.VERTIPORTS]
        placed = 0
        guard = 0
        while placed < n_ops and guard < n_ops * 8:
            guard += 1
            origin, dest = self.rng.sample(node_ids, 2)
            vehicle = "evtol" if self.rng.random() < 0.45 else "drone"
            # Spread departures so many are already airborne "now".
            t_req = now - 480 + self.rng.random() * 1500
            op, _ = plan_operation(origin, dest, vehicle, t_req)
            if op:
                with self._lock:
                    self.ops.append(op)
                placed += 1
        self._seed_external(now)
        return placed

    def _seed_external(self, now: float):
        """Non-cooperative crewed traffic (ADS-B-style) for see-and-avoid."""
        legs = [
            ("VP_DT", "VP_PL", "evtol"),
            ("VP_FW", "VP_RC", "evtol"),
            ("VP_AR", "VP_LF", "drone"),
        ]
        for o, d, v in legs:
            route = plan_route(o, d) or [o, d]
            op = Operation(v, route, now - 300 + self.rng.random() * 600,
                           operator="N-Registered", source="ADS-B")
            op.rid["ua_type"] = "helicopter"
            op.summary["cruise_alt_m"] += 200  # crewed traffic above the UTM deck
            for s in op.display:
                s["alt"] += 200
            with self._lock:
                self.external.append(op)

    # ---- queries ------------------------------------------------------
    def all_ops(self):
        with self._lock:
            return list(self.ops) + list(self.external)

    def snapshot(self, now: Optional[float] = None):
        now = now or time.time()
        out = []
        for op in self.all_ops():
            st = op.state_at(now)
            if st:
                out.append({
                    "id": op.id, "callsign": op.callsign, "vehicle": op.vehicle,
                    "operator": op.operator, "source": op.source,
                    "color": VEHICLES[op.vehicle]["color"], **{k: round(v, 6) if isinstance(v, float) else v for k, v in st.items()},
                    "alt_ft": round(st["alt"] / FT),
                })
        return {"now": now, "count": len(out), "aircraft": out}

    def add_live_operation(self, vehicle=None):
        """Inject a new deconflicted operation departing shortly (for SSE)."""
        node_ids = [v["id"] for v in airspace.VERTIPORTS]
        for _ in range(12):
            origin, dest = self.rng.sample(node_ids, 2)
            v = vehicle or ("evtol" if self.rng.random() < 0.45 else "drone")
            op, res = plan_operation(origin, dest, v, time.time() + 20)
            if op:
                with self._lock:
                    self.ops.append(op)
                self._emit({"event": "operation_added", "operation": op.to_dict(),
                            "resolution": res})
                return op
        return None

    # ---- background driver -------------------------------------------
    def start(self, period: float = 25.0):
        if self._running:
            return
        self._running = True

        def loop():
            while self._running:
                time.sleep(period)
                if not self._running:
                    break
                # Retire finished ops + spawn a fresh one to keep the sky busy.
                now = time.time()
                with self._lock:
                    for op in [o for o in self.ops if o.t_end < now - 30]:
                        ENGINE.release(op.id)
                        self.ops.remove(op)
                self.add_live_operation()
                self._emit({"event": "tick", **self.snapshot()})

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False


TRAFFIC = TrafficManager()
