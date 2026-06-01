"""Skyway — autonomous drone + eVTOL sky-roadmap / UTM system.

A self-contained airspace coordination prototype:

  * 4D strategic deconfliction (lat/lon + altitude + time), modelled on the
    FAA UAS Traffic Management (UTM) operational-intent-volume concept.
  * A "sky roadmap" of aerial corridors (skylanes) between vertiports with
    cardinal-direction altitude bands.
  * FAA data-share adapters (UAS Facility Maps, NOTAMs, TFRs, Remote ID) with
    live-API hooks and a deterministic offline simulation fallback.
  * A real-time 4D web view (CesiumJS) showing every other aircraft in the air.

Run:  python -m skyway.server   ->   http://localhost:5057
"""

__version__ = "0.1.0"
