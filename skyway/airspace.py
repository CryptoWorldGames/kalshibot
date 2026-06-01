"""Static airspace structure + FAA data-share adapters.

Scenario region: the Dallas-Fort Worth metroplex (matches the project's CDT
timezone). Everything here is data the rest of the system reasons over:

  * VERTIPORTS  - takeoff/landing nodes for drones & eVTOLs.
  * SKYLANES    - directed aerial corridors forming the "sky roadmap".
  * UASFM grid  - FAA UAS Facility Map ceilings (max AGL) near airports.
  * Advisories  - NOTAMs / TFRs as 4D no-fly volumes.

FAA adapters try the real endpoints when credentials are present in the
environment, otherwise they fall back to a deterministic local simulation so
the system runs fully offline.
"""

from __future__ import annotations

import os
import time
from typing import List

from . import geo

FT = geo.FT

# Region bounds (lat/lon) used for the UASFM grid and map framing.
REGION = {"lat_min": 32.55, "lat_max": 33.05, "lon_min": -97.20, "lon_max": -96.55}
REGION_CENTER = (32.80, -96.87)

# Airports whose UAS Facility Maps drive grid ceilings (FAA UASFM concept).
AIRPORTS = [
    {"id": "DFW", "name": "Dallas/Fort Worth Intl", "lat": 32.8998, "lon": -97.0403, "radius_km": 9.0},
    {"id": "DAL", "name": "Dallas Love Field", "lat": 32.8471, "lon": -96.8518, "radius_km": 6.5},
    {"id": "ADS", "name": "Addison", "lat": 32.9686, "lon": -96.8364, "radius_km": 4.0},
]

# ---------------------------------------------------------------------------
# Vertiports — nodes of the sky roadmap
# ---------------------------------------------------------------------------
VERTIPORTS = [
    {"id": "VP_DT", "name": "Downtown Dallas Vertiport", "lat": 32.7800, "lon": -96.7970, "elev_m": 130},
    {"id": "VP_LF", "name": "Love Field Vertistop", "lat": 32.8430, "lon": -96.8400, "elev_m": 148},
    {"id": "VP_PL", "name": "Plano North Hub", "lat": 33.0150, "lon": -96.7000, "elev_m": 200},
    {"id": "VP_AR", "name": "Arlington Stadium Pad", "lat": 32.7473, "lon": -97.0945, "elev_m": 184},
    {"id": "VP_IR", "name": "Las Colinas / Irving", "lat": 32.8740, "lon": -96.9420, "elev_m": 140},
    {"id": "VP_RC", "name": "Richardson Tech Pad", "lat": 32.9483, "lon": -96.7299, "elev_m": 196},
    {"id": "VP_FW", "name": "Fort Worth Medical", "lat": 32.7300, "lon": -97.3300, "elev_m": 200},
    {"id": "VP_GP", "name": "Grand Prairie Logistics", "lat": 32.7460, "lon": -96.9980, "elev_m": 170},
]
VP = {v["id"]: v for v in VERTIPORTS}

# Directed corridors. Altitude band assigned later by cardinal heading so that
# opposing traffic is naturally vertically separated (an FAA-style hemispheric
# rule applied to low-altitude UAS).
_LANE_PAIRS = [
    ("VP_DT", "VP_LF"), ("VP_LF", "VP_IR"), ("VP_IR", "VP_AR"),
    ("VP_AR", "VP_GP"), ("VP_GP", "VP_DT"), ("VP_DT", "VP_RC"),
    ("VP_RC", "VP_PL"), ("VP_PL", "VP_RC"), ("VP_IR", "VP_FW"),
    ("VP_FW", "VP_AR"), ("VP_LF", "VP_RC"), ("VP_GP", "VP_IR"),
]

# Vertical bands keyed by quadrant of corridor heading (metres MSL-ish AGL band).
# Eastbound/Northbound vs Westbound/Southbound get different floors -> the
# "lanes of the sky" never share altitude head-on.
def _band_for_heading(hdg: float):
    if 315 <= hdg or hdg < 45:        # northbound
        return (90, 120)
    if 45 <= hdg < 135:               # eastbound
        return (120, 150)
    if 135 <= hdg < 225:              # southbound
        return (60, 90)
    return (30, 60)                    # westbound


def build_skylanes() -> List[dict]:
    lanes = []
    for i, (a, b) in enumerate(_LANE_PAIRS):
        va, vb = VP[a], VP[b]
        hdg = geo.bearing(va["lat"], va["lon"], vb["lat"], vb["lon"])
        floor, ceil = _band_for_heading(hdg)
        lanes.append({
            "id": f"SL{i:02d}",
            "from": a,
            "to": b,
            "heading": round(hdg, 1),
            "length_km": round(geo.haversine(va["lat"], va["lon"], vb["lat"], vb["lon"]) / 1000, 2),
            "floor_m": floor,
            "ceil_m": ceil,
            "width_m": 120,
            "path": [[va["lat"], va["lon"]], [vb["lat"], vb["lon"]]],
        })
    return lanes


SKYLANES = build_skylanes()
LANES_BY_NODE = {}
for _l in SKYLANES:
    LANES_BY_NODE.setdefault(_l["from"], []).append(_l)


# ---------------------------------------------------------------------------
# UAS Facility Map grid — max allowed AGL altitude per cell (FAA UASFM)
# ---------------------------------------------------------------------------
def build_uasfm_grid(step_deg: float = 0.02) -> List[dict]:
    cells = []
    lat = REGION["lat_min"]
    while lat < REGION["lat_max"]:
        lon = REGION["lon_min"]
        while lon < REGION["lon_max"]:
            clat, clon = lat + step_deg / 2, lon + step_deg / 2
            ceiling_ft = 400  # default Part 107 ceiling away from airports
            for ap in AIRPORTS:
                d_km = geo.haversine(clat, clon, ap["lat"], ap["lon"]) / 1000
                if d_km < ap["radius_km"]:
                    # Stepped UASFM ceilings: 0 at field, rising outward.
                    ratio = d_km / ap["radius_km"]
                    ceiling_ft = min(ceiling_ft, int(round((ratio * 400) / 50) * 50))
            cells.append({
                "lat_min": round(lat, 4), "lon_min": round(lon, 4),
                "lat_max": round(lat + step_deg, 4), "lon_max": round(lon + step_deg, 4),
                "ceiling_ft": ceiling_ft,
                "ceiling_m": round(ceiling_ft * FT, 1),
            })
            lon += step_deg
        lat += step_deg
    return cells


UASFM_GRID = build_uasfm_grid()


def uasfm_ceiling_m(lat: float, lon: float) -> float:
    """Lowest UASFM ceiling (metres AGL) applicable at a point."""
    ceiling = 400 * FT
    for ap in AIRPORTS:
        d_km = geo.haversine(lat, lon, ap["lat"], ap["lon"]) / 1000
        if d_km < ap["radius_km"]:
            ceiling = min(ceiling, (d_km / ap["radius_km"]) * 400 * FT)
    return ceiling


# ---------------------------------------------------------------------------
# FAA data-share adapters (NOTAM / TFR / Remote ID)
# ---------------------------------------------------------------------------
class FAADataShare:
    """Pluggable access to FAA-required data feeds.

    Live mode is enabled when FAA_NOTAM_CLIENT_ID / FAA_NOTAM_CLIENT_SECRET are
    set (FAA NOTAM API). Without them, deterministic simulated advisories are
    returned so deconfliction and the map still have hazards to reason about.
    """

    def __init__(self):
        self.notam_id = os.environ.get("FAA_NOTAM_CLIENT_ID")
        self.notam_secret = os.environ.get("FAA_NOTAM_CLIENT_SECRET")
        self.live = bool(self.notam_id and self.notam_secret)
        self._cache = None
        self._cache_t = 0.0

    @property
    def mode(self) -> str:
        return "live-faa" if self.live else "simulation"

    def advisories(self) -> List[dict]:
        """4D no-fly / caution volumes (NOTAMs + TFRs)."""
        now = time.time()
        if self._cache and now - self._cache_t < 60:
            return self._cache
        data = self._fetch_live() if self.live else None
        if not data:
            data = self._simulated()
        self._cache, self._cache_t = data, now
        return data

    def _fetch_live(self):
        try:  # pragma: no cover - requires real FAA credentials + network
            import requests
            tok = requests.post(
                "https://external-api.faa.gov/oauth/token",
                data={"grant_type": "client_credentials"},
                auth=(self.notam_id, self.notam_secret), timeout=10,
            ).json().get("access_token")
            r = requests.get(
                "https://external-api.faa.gov/notamapi/v1/notams",
                headers={"Authorization": f"Bearer {tok}"},
                params={"responseFormat": "geoJson",
                        "locationLatitude": REGION_CENTER[0],
                        "locationLongitude": REGION_CENTER[1],
                        "locationRadius": 25},
                timeout=12,
            )
            return self._parse_faa_geojson(r.json())
        except Exception:
            return None

    @staticmethod
    def _parse_faa_geojson(_payload):
        # Real FAA NOTAM GeoJSON parsing would live here; left minimal because
        # the public schema requires an approved API account to exercise.
        return None

    def _simulated(self):
        # Positioned in the gaps *between* vertiports so they block crossing
        # traffic (demonstrating refusal / reroute) without sitting on a launch
        # pad, which would make that vertiport permanently un-departable.
        now = time.time()
        return [
            {
                "id": "NFZ-1934", "type": "No-Fly Zone", "label": "Stadium event — No-Fly Zone",
                "kind": "circle", "lat": 32.7900, "lon": -97.0200, "radius_m": 1700,
                "alt_min_m": 0, "alt_max_m": 700,
                "t_start": now - 600, "t_end": now + 3600 * 3,
                "severity": "prohibited",
            },
            {
                "id": "NFZ-7A21", "type": "Caution", "label": "Tall crane — caution area",
                "kind": "circle", "lat": 32.9483, "lon": -96.7299, "radius_m": 600,
                "alt_min_m": 0, "alt_max_m": 130,
                "t_start": now - 3600, "t_end": now + 3600 * 24,
                "severity": "caution",
            },
            {
                "id": "NFZ-5C09", "type": "No-Fly Zone", "label": "Restricted area — No-Fly Zone",
                "kind": "circle", "lat": 32.9020, "lon": -96.9000, "radius_m": 1400,
                "alt_min_m": 0, "alt_max_m": 500,
                "t_start": now - 1800, "t_end": now + 3600 * 6,
                "severity": "prohibited",
            },
        ]


FAA = FAADataShare()


def lane_crosses_prohibited(lane: dict) -> bool:
    """True if a corridor's tube intersects any currently-prohibited advisory.

    Used by route planning to steer the network *around* TFRs rather than only
    time-shifting (which can never clear a standing no-fly area).
    """
    now = time.time()
    a0 = lane["path"][0]
    b0 = lane["path"][1]
    half_w = lane["width_m"] / 2
    for a in FAA.advisories():
        if a["severity"] != "prohibited":
            continue
        if not (a["t_start"] <= now + 1800 <= a["t_end"] or a["t_start"] <= now <= a["t_end"]):
            continue
        # altitude band overlap
        if a["alt_max_m"] < lane["floor_m"] or a["alt_min_m"] > lane["ceil_m"]:
            continue
        if a["kind"] == "circle":
            d = geo.point_to_segment_m(a["lat"], a["lon"], a0[0], a0[1], b0[0], b0[1])
            if d <= a["radius_m"] + half_w:
                return True
    return False


def advisory_blocks(pt) -> bool:
    """True if 4D point (lat,lon,alt,t) falls inside a prohibited advisory."""
    lat, lon, alt, t = pt
    for a in FAA.advisories():
        if a["severity"] != "prohibited":
            continue
        if not (a["t_start"] <= t <= a["t_end"]):
            continue
        if not (a["alt_min_m"] <= alt <= a["alt_max_m"]):
            continue
        if a["kind"] == "circle" and geo.haversine(lat, lon, a["lat"], a["lon"]) <= a["radius_m"]:
            return True
    return False
