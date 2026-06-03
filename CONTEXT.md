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
- **Bot scans every 15 seconds when you're watching, 30 seconds when away** (adaptive)
- Portfolio refreshes every 60s (throttled)
- Monitor runs every 45s (backend, always on while Flask running)
- GitHub primary source of truth; OneDrive backup only

---

## 2026-06-01 Session 3 — Adaptive scan interval

- **Adaptive scan interval deployed** (index.html): 
  - 15s when you're actively watching (page visible)
  - 30s when you step away (page hidden/tabbed out)
  - Keeps scanning in both cases (doesn't pause)
  - Balances market detection latency vs API load
- **Console logging added** to verify interval changes (📵 and 📱 emoji markers)
- **All updates pushed to GitHub** (commits: 7fe5608, 3fc998e)
- Bot is live and responsive with 9 open positions

*Last updated: 2026-06-01*
*GitHub: https://github.com/CryptoWorldGames/kalshibot*

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

---

## 2026-06-03 — Phone-optimized mobile page (`/mobile`)

- **New file `mobile.html` + route `/mobile`** (app.py, next to `index()`, same no-cache
  headers). Phone-first UI on the SAME backend/account — open
  `http://<PC-tailscale-ip>:5000/mobile`. Built on a separate "mobile" branch and rebased
  onto latest main, so **desktop `index.html` is untouched** (byte-identical to main).
- Two bottom-nav tabs: **Scan** (presets for win-prob + ends-within, tap **Buy**) and
  **Positions** (tap **Sell All**). Uses `/api/scan`, `/api/buy`, `/api/sell`,
  `/api/portfolio`. Buy/sell reference markets by array index (apostrophe-safe).
- **MANUAL buy/sell only — NO auto-buy loop** (deliberate: avoids two "Start Bot" loops
  fighting over the one Kalshi account). Backend 45s sell-monitor still runs as always.
- **Bet amount:** NO forced default. Preset chips 25¢–$5 + free type-in, persisted to
  localStorage (`kb_mobile_v1`) with a 💾 Save button. Warns when amount > $5 (server
  `KALSHI_MAX_PER_MARKET` cap clamps buys to $5).
- Remote access: user reaches PC over **Tailscale** (`100.110.168.114`), works off home WiFi
  too (cellular). Goes live only after the PC does `git pull` + restart of Flask.

*Last updated: 2026-06-03*
