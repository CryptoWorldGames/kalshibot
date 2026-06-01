# eVTOL Maps — air trip planner

**Google-Maps-for-the-air.** A real point-to-point **trip-planning** system any
eVTOL air taxi or delivery drone can use to plan a route between landing pads and
airports over real-world airspace — with **ETA, altitude profile, energy/range
check, no-fly-zone avoidance, FAA data, and live traffic**.

It is **not a game**. The optional 3D "Preview flight" simply flies the planned
route so you can see it from the cockpit before departure.

> Scenario region: the Dallas–Fort Worth metroplex. Runs fully offline — no API
> keys, no Cesium ion token (OpenStreetMap imagery).

```bash
pip install flask
python -m evtol_maps.server        # -> http://localhost:5057
```

> ⚠️ Temporarily living inside the `kalshibot` repo under `evtol-maps/` for
> organization. It is self-contained and intended to move to its own repo.

---

## How it works

1. Pick **From** / **To** pads (or swap), an **aircraft**, and a departure time.
2. **Plan trip** → the engine routes through the aerial corridor network,
   strategically deconflicts the path in **4D** (lat/lon + altitude + time)
   against other traffic, and **re-routes around active No-Fly Zones**.
3. You get a full plan: **distance, flight time, cruise altitude, energy use vs
   range, departure/arrival times (CDT)**, deconfliction delay, and a
   **leg-by-leg itinerary** (heading + distance + time per leg).
4. **Preview flight** (optional) flies the route in 3D — cockpit or chase (press
   **C**) — purely to visualize it.

## What's real here

| Piece | Description |
|---|---|
| **Route network** | Directed aerial corridors between vertiports with cardinal-direction altitude bands. |
| **4D deconfliction** | Trips are separated in lat/lon **+ altitude + time** (150 m / 25 m / 8 s), the FAA UTM model. |
| **No-fly avoidance** | Routing drops corridors that intersect active prohibited zones (TFRs). |
| **FAA data shares** | UAS Facility Map ceilings, NOTAMs/TFRs, Remote ID. Live FAA NOTAM API when `FAA_NOTAM_CLIENT_ID`/`_SECRET` are set; otherwise an offline simulation. |
| **Range / energy** | Per-aircraft `range_km` drives the energy-use estimate and out-of-range warnings. |
| **Live traffic** | Other drones, eVTOLs and crewed (ADS-B) aircraft shown on the map. |

## Architecture

```
evtol-maps/
├── requirements.txt
└── evtol_maps/
    ├── geo.py            great-circle / 4D separation math (stdlib only)
    ├── airspace.py       vertiports, corridors, UASFM grid, FAA data adapters
    ├── deconfliction.py  4D reservation + separation engine
    ├── traffic.py        route planning, trip generation, vehicles, live traffic
    ├── server.py         Flask REST API
    └── static/index.html CesiumJS trip-planner UI (tokenless OSM)
```

## API (key endpoints)

| Method | Route | Purpose |
|---|---|---|
| `GET`  | `/api/airspace` | Landing pads, corridors, airports, UASFM grid |
| `GET`  | `/api/faa/advisories` | NOTAMs / TFRs (no-fly zones) |
| `POST` | `/api/route/plan` | Plan a trip: `{origin,dest,vehicle,depart_in_s}` → 4D route + ETA |
| `GET`  | `/api/operations` | All trips with 4D trajectories |
| `GET`  | `/api/traffic` | Aircraft airborne right now |
| `GET`  | `/api/stats` | Counts + vehicle profiles (incl. range) |

> Prototype for planning/visualization — not a certified UTM service and not
> connected to live air traffic; do not use for real flight operations.
