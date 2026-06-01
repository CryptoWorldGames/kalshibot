"""4D strategic deconfliction.

Each operation reserves a 4D tube: a time-stamped sequence of (lat, lon, alt, t)
samples plus a protected radius. Before an operation is accepted, its candidate
trajectory is checked against:

  * every already-reserved trajectory (min separation in space AND time), and
  * prohibited FAA advisory volumes (NOTAM/TFR).

On conflict the planner first tries shifting the departure time later (the
cheapest fix), then nudging the cruise altitude band. This mirrors the FAA UTM
"strategic deconfliction before flight" model.
"""

from __future__ import annotations

import threading
from typing import List, Optional

from . import geo, airspace

# Minimum separation standards for the protected volume.
SEP_H = 150.0   # metres horizontal
SEP_V = 25.0    # metres vertical
SEP_T = 8.0     # seconds — samples further apart in time never conflict


class Reservation:
    __slots__ = ("op_id", "samples")

    def __init__(self, op_id: str, samples: List[tuple]):
        self.op_id = op_id
        self.samples = samples  # list of (lat, lon, alt, t)


class Deconflictor:
    def __init__(self):
        self._lock = threading.RLock()
        self._reservations: List[Reservation] = []

    def reset(self):
        with self._lock:
            self._reservations.clear()

    def reservations(self):
        with self._lock:
            return list(self._reservations)

    def _conflict(self, samples, ignore_op=None) -> Optional[dict]:
        """Return details of the first conflicting sample, or None if clear."""
        # Advisory (NOTAM/TFR) check.
        for s in samples:
            if airspace.advisory_blocks(s):
                return {"reason": "advisory", "lat": s[0], "lon": s[1], "alt": s[2], "t": s[3]}
        # Traffic check against existing reservations.
        with self._lock:
            res = [r for r in self._reservations if r.op_id != ignore_op]
        for r in res:
            for s in samples:
                for o in r.samples:
                    if abs(s[3] - o[3]) >= SEP_T:
                        continue
                    if not geo.separation_ok(s, o, SEP_H, SEP_V, SEP_T):
                        return {"reason": "traffic", "with": r.op_id,
                                "lat": s[0], "lon": s[1], "alt": s[2], "t": s[3]}
        return None

    def check(self, samples, ignore_op=None) -> Optional[dict]:
        return self._conflict(samples, ignore_op=ignore_op)

    def reserve(self, op_id: str, samples: List[tuple]):
        with self._lock:
            self._reservations = [r for r in self._reservations if r.op_id != op_id]
            self._reservations.append(Reservation(op_id, samples))

    def release(self, op_id: str):
        with self._lock:
            self._reservations = [r for r in self._reservations if r.op_id != op_id]


# Module-level singleton shared by the planner + simulator.
ENGINE = Deconflictor()
