"""Geospatial + 4D math helpers (stdlib only).

Distances in metres, angles in degrees, altitudes in metres MSL unless noted.
A "4D point" is (lat, lon, alt_m, t_epoch). Separation is checked in all four
dimensions: horizontal, vertical, and time.
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence

EARTH_R = 6_371_000.0  # mean Earth radius, metres
FT = 0.3048            # one foot in metres


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_R * math.asin(min(1.0, math.sqrt(a)))


def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing from point 1 to point 2, degrees [0, 360)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def destination(lat: float, lon: float, bearing_deg: float, dist_m: float):
    """Point reached by travelling dist_m from (lat,lon) on the given bearing."""
    d = dist_m / EARTH_R
    br = math.radians(bearing_deg)
    p1 = math.radians(lat)
    l1 = math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(br))
    l2 = l1 + math.atan2(
        math.sin(br) * math.sin(d) * math.cos(p1),
        math.cos(d) - math.sin(p1) * math.sin(p2),
    )
    return math.degrees(p2), (math.degrees(l2) + 540.0) % 360.0 - 180.0


def polyline_length(pts: Sequence[Sequence[float]]) -> float:
    """Total ground length of a polyline of (lat, lon[, ...]) points, metres."""
    total = 0.0
    for a, b in zip(pts, pts[1:]):
        total += haversine(a[0], a[1], b[0], b[1])
    return total


def interpolate_along(pts: Sequence[Sequence[float]], frac: float):
    """Point at fractional distance `frac` (0..1) along a lat/lon polyline.

    Returns (lat, lon, heading_deg).
    """
    frac = max(0.0, min(1.0, frac))
    total = polyline_length(pts)
    if total == 0:
        return pts[0][0], pts[0][1], 0.0
    target = total * frac
    acc = 0.0
    for a, b in zip(pts, pts[1:]):
        seg = haversine(a[0], a[1], b[0], b[1])
        if acc + seg >= target or seg == 0:
            f = 0.0 if seg == 0 else (target - acc) / seg
            br = bearing(a[0], a[1], b[0], b[1])
            lat, lon = destination(a[0], a[1], br, seg * f)
            return lat, lon, br
        acc += seg
    return pts[-1][0], pts[-1][1], bearing(pts[-2][0], pts[-2][1], pts[-1][0], pts[-1][1])


def point_to_segment_m(plat, plon, alat, alon, blat, blon) -> float:
    """Horizontal distance from point P to segment A-B, metres (local planar)."""
    mlat = math.radians((alat + blat) / 2)
    sx = math.cos(mlat) * EARTH_R * math.pi / 180.0  # metres per degree lon
    sy = EARTH_R * math.pi / 180.0                    # metres per degree lat
    ax, ay = alon * sx, alat * sy
    bx, by = blon * sx, blat * sy
    px, py = plon * sx, plat * sy
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    if seg2 == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy)


def point_in_poly(lat: float, lon: float, poly: Sequence[Sequence[float]]) -> bool:
    """Ray-cast point-in-polygon for a ring of (lat, lon) vertices."""
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        yi, xi = poly[i][0], poly[i][1]
        yj, xj = poly[j][0], poly[j][1]
        if ((yi > lat) != (yj > lat)) and (
            lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi
        ):
            inside = not inside
        j = i
    return inside


def separation_ok(a, b, sep_h: float, sep_v: float, sep_t: float) -> bool:
    """True if two 4D points (lat,lon,alt,t) are *clear* of each other.

    Conflict = inside the protected volume in ALL of horizontal, vertical and
    time simultaneously. If any dimension is separated, they are clear.
    """
    if abs(a[3] - b[3]) >= sep_t:
        return True
    if abs(a[2] - b[2]) >= sep_v:
        return True
    if haversine(a[0], a[1], b[0], b[1]) >= sep_h:
        return True
    return False
