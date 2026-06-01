# Skyway — Autonomous Drone & eVTOL 4D Airspace System

A self-contained prototype of a **UAS Traffic Management (UTM)** "sky roadmap":
it plans and strategically deconflicts autonomous **drone** and **eVTOL** flights
in **4D** (latitude · longitude · altitude · **time**), synchronises with the
**FAA-required data shares**, and shows **everyone else in the air in real time**
on a CesiumJS globe whose clock/timeline lets you scrub through time.

> Scenario region: the Dallas–Fort Worth metroplex (matches the project's CDT
> timezone). Fully runnable offline — no API keys or Cesium ion token required.

```bash
pip install flask
python -m skyway.server        # -> http://localhost:5057
```

---

## What it does

| Capability | How |
|---|---|
| **Sky roadmap** | A directed network of aerial **corridors (skylanes)** between vertiports, with cardinal-direction **altitude bands** so opposing traffic is vertically separated. |
| **4D strategic deconfliction** | Every operation reserves a time-stamped 4D tube. New flights are checked for **horizontal + vertical + time** separation against all reservations and refused/adjusted before takeoff — the FAA UTM "deconfliction before flight" model. |
| **Automatic resolution** | On conflict the planner first **delays** departure (time-shift), then nudges the **cruise altitude band**, then **reroutes** around prohibited zones. |
| **FAA data shares** | Adapters for **UAS Facility Maps** (UASFM altitude ceilings), **NOTAMs**, **TFRs**, and **Remote ID** (Part 89). Live FAA NOTAM API is used when credentials are present, otherwise a deterministic simulation. |
| **Who's in the air, in 4D** | Live drones, eVTOLs and non-cooperative **crewed (ADS-B) traffic** rendered as time-dynamic Cesium entities. Drag the timeline to see the airspace at any moment, past or future. |
| **Real-time sync** | Server-Sent-Events stream pushes newly cleared operations and live snapshots to every connected client. |

---

## Architecture

```
skyway/
├── geo.py            great-circle / 4D separation math (stdlib only)
├── airspace.py       vertiports, skylanes, UASFM grid, FAA data-share adapters
├── deconfliction.py  4D reservation store + separation engine
├── traffic.py        route planning, trajectory generation, simulator, Remote ID
├── server.py         Flask REST API + SSE stream
└── static/index.html CesiumJS 4D client (tokenless: OSM imagery)
```

### Separation standards (configurable in `deconfliction.py`)
`150 m` horizontal · `25 m` vertical · `8 s` temporal.

---

## REST API

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/api/airspace` | Vertiports, skylanes, UASFM ceiling grid, airports |
| `GET` | `/api/faa/advisories` | NOTAMs / TFRs as 4D no-fly volumes |
| `GET` | `/api/operations` | All operations with full 4D trajectories |
| `GET` | `/api/traffic` | Snapshot of every aircraft airborne *now* |
| `POST` | `/api/route/plan` | Request clearance: `{origin,dest,vehicle,depart_in_s}` |
| `POST` | `/api/operations/add` | Inject a random deconflicted operation |
| `POST` | `/api/scenario/reset` | Reseed the simulation |
| `GET` | `/api/stream` | SSE: live ticks + newly cleared operations |
| `GET` | `/api/stats` | Counts + data-share mode |

### Example — request a clearance
```bash
curl -X POST localhost:5057/api/route/plan -H 'Content-Type: application/json' \
  -d '{"origin":"VP_FW","dest":"VP_PL","vehicle":"evtol","depart_in_s":30}'
# -> {"ok":true,"resolution":{"status":"cleared","delay_s":60,"alt_bump_m":0,
#     "route":["VP_FW","VP_AR","VP_GP","VP_DT","VP_RC","VP_PL"]}, "operation":{...}}
```

---

## Going live with real FAA data

The data-share layer auto-switches to **live** mode when these environment
variables are set (FAA NOTAM API credentials):

```bash
export FAA_NOTAM_CLIENT_ID=...
export FAA_NOTAM_CLIENT_SECRET=...
```

Real integrations a production deployment would extend (`airspace.FAADataShare`):

* **LAANC / UAS Facility Maps** via an FAA-approved UAS Service Supplier (USS).
* **Remote ID** ingest from a Network Remote ID provider.
* **TFR / NOTAM** from the FAA NOTAM API + DroneZone.
* **SWIM** (System Wide Information Management) for surveillance feeds.
* **Inter-USS** strategic-deconfliction exchange (ASTM F3548 UTM).

These hooks are stubbed where a real account/network is required; the offline
simulation keeps the whole system exercisable without them.

---

## Notes

* The 4D view uses CesiumJS from a CDN with OpenStreetMap imagery and the
  ellipsoid (no terrain token), so it renders with **no Cesium ion account**.
* This is a **simulation/planning prototype**, not a certified UTM service. It is
  not connected to live air traffic and must not be used for real flight ops.
