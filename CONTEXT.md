# KalshiBot — Full Context File

> **For future Claude instances:** Read this file AND the GitHub repo at https://github.com/CryptoWorldGames/kalshibot
> This is the authoritative context. Update this file at the end of every session.

---

## Project Overview

Flask + single-page HTML trading bot for [Kalshi](https://kalshi.com) prediction markets.
**Goal:** Scan, auto-buy, and auto-sell high-probability short-expiry contracts.
**Run:** `python app.py` → open `http://localhost:5000`
**GitHub:** https://github.com/CryptoWorldGames/kalshibot

---

## Key Files

| File | Purpose |
|------|---------|
| `app.py` | Flask backend — API, scan, buy/sell, monitor thread |
| `index.html` | Single-page frontend — all UI |
| `bot_positions.json` | Open/closed positions tracker |
| `bot_strategy.json` | Persisted sell strategy (survives Flask restarts) |
| `CONTEXT.md` | This file — cross-device/session memory |

**Credentials:** `C:\Users\mycry\OneDrive\Documents\claude code\kalshi-keys\`
- `kalshi_api_key` — UUID
- `test2.txt` — RSA private key PEM
- **NEVER read these files**

---

## Architecture

### Backend (`app.py`)
- **Auth:** RSA-PSS signs `timestamp_ms + METHOD + path`
- **Monitor thread:** runs every 45s, auto-sells open positions based on sell strategy
- **Market cache:** `_get_market(ticker)` — 60s cache, failed lookups cached 5min
- **Balance:** `balance_dollars` = available cash, `portfolio_value` (cents) = open positions
- **Sell strategy stored in:** `bot_strategy.json` (persists across restarts)

### Key Endpoints
| Endpoint | Purpose |
|----------|---------|
| `/api/scan` | Find markets matching filters |
| `/api/buy` | Buy contracts |
| `/api/sell` | Sell contracts (includes price field for Kalshi) |
| `/api/portfolio` | Balance + positions + settlements |
| `/api/strategy` | Update sell strategy |
| `/api/stats` | Performance stats (uses Kalshi settlements API) |
| `/api/coach` | AI strategy recommendations |

### Frontend (`index.html`)
- **Tabs:** Scanner, Positions, Stats, Coach
- **Bot:** Start Bot / Stop Bot FAB (bottom right, Scanner tab only when stopped)
- **Auto-save:** All settings save on blur/change to localStorage
- **Tab restore:** Remembers last active tab across refreshes
- **Bot auto-restart:** If bot was running when page refreshed, restarts automatically

---

## Sell Strategies (4 options)

1. ⏳ **Wait for resolution** — hold until market closes
2. 📈 **Sell at % profit** — sell when profit hits X%
3. 💵 **Sell at $ profit** — sell when TOTAL profit reaches $X
4. 🎯 **Sell when price hits X¢** — buy at 25¢, set 35¢, sells when bid reaches 35¢

**Monitor checks ALL open tracked positions every 45s regardless of browser bot state.**
Strategy pushed to Flask on every page load (1 second after load).

---

## Positions Table Columns
Market | QTY | Bought@ | Spent | Value Now | Profit Now | Max Profit | Captured | Time Left | Sell button

**Sell button shows:** "Sell All $1.55 / (-$1.40 loss)" — total receive + profit/loss

---

## Settings System
- **Save Settings** button top-right of Filters card
- **⭐ Saved Strategies** button — 10 named memory slots (save/load all settings)
- All inputs auto-save on blur (click away)
- Settings key: `kb_settings_v2` in localStorage

---

## Scanner Settings
- Win probability range: presets (80-95%, 85-98%, 90-98%, 95-98%) + custom input
- Ends within: 5m to 24h
- Started: at least/at most X min ago
- Skip if too close to end: X min
- Categories: Crypto, Sports, Politics, Economics, Entertainment, Weather, NFL, NBA, MLB, NHL, Soccer, Golf, Tennis, MMA/UFC, Combo/Parlay (all checked by default)
- Active only (skip thin), YES side only
- Max spend per purchase, Max buys per game, Max buys per scan cycle, Hold max positions

## Auto Mode
- Run for minutes / buys / Until stopped
- Stop if: profit $ or %, loss $ or %
- When "Until stopped" and cash runs out: pauses 1 min, countdown shown, auto-resumes
- Bot saved state in `kb_bot_was_running` localStorage — auto-restarts on page reload

---

## Known Issues / Behaviors

- **Stats "Loading..."** — Kalshi settlements API sometimes times out (15s timeout added)
- **Popups:** Sold/resolved popups show 10s max, 3 max stacked
- **"Market resolved" vs "Auto-sold"** — popup correctly distinguishes
- **Dead links:** Some Kalshi election market URLs 404 (their URL structure varies)
- **Scan finding nothing:** With `good_liq=true` + low-odds markets, bids are 0 and fail live-bid check. Use 85-96% range for reliable finds.
- **Near loss positions:** Monitor holds if bid ≤ 3¢ YES or bid = 0 (no buyers)
- **tracked fallback spam:** Suppressed — failed market lookups cached 5min

---

## GitHub Workflow
```bash
# On any machine:
git pull                    # get latest
# ... make changes ...
git add -A
git commit -m "description"
git push
```

---

## User Preferences
- CDT timezone (America/Chicago) everywhere
- No comments in code unless truly non-obvious
- Concise responses
- Max $1 per bet default
- Bot scans every 10 seconds when running
- Portfolio refreshes every 60s (throttled)
- Monitor runs every 45s (backend, always on while Flask running)

---

## 2026-05-31 Session 2 — Start/Stop fix + why crypto wasn't buying

- **Start/Stop button "restarts instead" — FIXED.** `syncFabButtons()` bailed early
  (`if (!fabAuto) return;`) because the FAB was removed in favor of bottom bar
  `#botControlBtn`. So the button text, status pill, and green "BOT RUNNING" banner were
  hardcoded and never reflected `autoModeOn`. Rewrote `syncFabButtons()` to drive all of them.
  Bottom bar is now green "▶ Start Bot" when stopped, red "⏹ Stop Bot" when running.
- **crypto_times=none blocked all crypto — FIXED** (app.py). Crypto checked + no time-type
  boxes → `none` → was `{"none"}` (matched nothing). Now `none`/empty → no sub-filter (all
  crypto). Scan total 1 → 11.
- **15-min crypto series added to KNOWN_SERIES** (app.py): KXBTC15M/KXETH15M/KXSOL15M/
  KXHYPE15M/KXDOGE15M/KXBNB15M/KXXRP15M (+30M/1H) so they're probed every cycle.
- **loadBalance timeout 8s → 15s** (index.html) — stops "AbortError" console spam.
- **WHY nothing bought at $0.05 (config, not a bug):** a nickel only affords contracts ≤5¢.
  Affordable 15-min crypto is usually the "no" side (2–5¢); with **Buy Down OFF** those are
  skipped, so it only bought the rare cheap "yes" market (e.g. politics @2¢). To buy 15-min
  crypto at $0.05, enable **Buy Down** or raise Max spend. ← user decision.
- Changes are LOCAL only (not committed/pushed yet).

---

## 2026-06-01 Session — Skyway: drone/eVTOL 4D airspace subsystem (NEW, separate from the trading bot)

Added a **self-contained `skyway/` package** on branch
`claude/drone-evtol-airspace-system-DhTg6`. It is unrelated to KalshiBot trading
— a standalone UAS Traffic Management (UTM) prototype. Documented here per the
"always update CONTEXT.md" rule.

- **What:** Autonomous drone + eVTOL "sky roadmap" — plans/strategically
  deconflicts flights in **4D** (lat·lon·alt·**time**), syncs FAA data shares
  (UAS Facility Maps, NOTAMs, TFRs, Remote ID), shows all other aircraft live on
  a CesiumJS globe with a scrubbable timeline.
- **Run:** `pip install flask` then `python -m skyway.server` → http://localhost:5057
- **Files:** `geo.py` (4D math), `airspace.py` (vertiports/skylanes/UASFM grid +
  FAA adapters), `deconfliction.py` (4D reservation engine, sep 150 m / 25 m / 8 s),
  `traffic.py` (route planning, trajectory gen, simulator, Remote ID),
  `server.py` (Flask REST + SSE), `static/index.html` (Cesium 4D UI, tokenless OSM).
- **Deconfliction:** time-shift → altitude bump → reroute around prohibited TFRs.
  Routing is advisory-aware (drops corridors crossing active TFRs).
- **FAA live mode:** auto-enables when `FAA_NOTAM_CLIENT_ID` / `_SECRET` env set;
  otherwise deterministic simulation so it runs fully offline.
- **Verified:** all REST endpoints + SSE tested; 40/56 vertiport pairs clear,
  remaining 16 are legitimate traffic-saturation refusals.
- **Note:** Flask was NOT in the container originally; installed for testing.
  `requirements.txt` already lists `flask>=3.0.0`. Scenario region = DFW metroplex.

*Last updated: 2026-06-01*
*GitHub: https://github.com/CryptoWorldGames/kalshibot*
