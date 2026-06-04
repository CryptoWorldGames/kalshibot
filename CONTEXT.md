# KalshiBot — Full Context File

> **For future Claude instances:** Read this file AND the GitHub repo at https://github.com/CryptoWorldGames/kalshibot
> This is the authoritative context. Update this file at the end of every session.

---

## Project Overview

Flask + single-page HTML trading bot for [Kalshi](https://kalshi.com) prediction markets.
**Goal:** Scan, auto-buy, and auto-sell high-probability short-expiry contracts.
**Run:** `python app.py` → open `http://localhost:5003`  (manager on 5103)
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

➕ **Stop loss (independent toggle, lives in the Sell Strategy card):** "Cut losses if a
position drops X%". Applies ON TOP of whichever exit above is selected (even "Wait for
resolution"). Stored in `sell_strategy["stop_loss_pct"]` (persisted to `bot_strategy.json`)
because that's the dict the monitor reads. The monitor still won't sell when bid ≤3¢ YES or
the book is empty (can't sell into no buyers).

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
- (Removed 2026-06-02) The old "Stop Loss — Auto-sell if you lose X" portfolio-level
  profit/loss auto-stop was deleted — it was mislabeled, overlapped RUN UNTIL, and its
  loss-sell path was broken. Stop-loss is now per-position in the Sell Strategy card.
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

## 2026-06-03 Session 3 — Settings persistence fixes + portfolio enrichment starvation fix

**Fixed three critical issues:**

1. **Portfolio enrichment starvation (app.py, line ~957-1056):** When portfolio had 1000+ 
   open positions and enrich=true, the `/api/portfolio` endpoint would loop through EVERY
   position calling `_get_market(ticker)` (expensive Kalshi API call), racing with the 
   scan loop for the global rate-limit lock (0.5s gap). This starved the scan and caused
   timeouts. **Fix:** Skip market enrichment when `len(raw_positions) >= 100`. Cached 
   balance data still shows cash/portfolio value. Enrichment available on-demand only.

2. **Settings persistence gaps in Sell Strategy card (index.html, line 1646-1681):** 
   Five pill/button functions were missing `saveSettings()` calls, so their values would 
   push to Flask backend but NOT persist to localStorage across hard refresh:
   - `setProfit(pct)` — clicking 5%, 10%, 15%, 20%, 25% profit buttons
   - `setProfitDol(dol)` — clicking $0.05, $0.10, $0.25, $0.50, $1.00 buttons
   - `setTargetPrice(cents)` — clicking 30¢, 40¢, 50¢, 60¢, 75¢, 90¢ buttons
   - `setBuyIn(cents)` — clicking buy-in price buttons (if used)
   - `profitCustom.oninput` — typing custom % value
   
   **Fix:** Added `saveSettings()` + `pushStrategy()` to each function so values sync to 
   localStorage immediately on click/type. NOTE: `profitDolCustom.oninput` already had 
   this (line 1690), so only the pill functions were missing it.

3. **Verified checkbox/input persistence:** All 21 checkboxes + 22 inputs in SETTINGS_CHECKBOXES
   and SETTINGS_INPUTS are correctly wired to auto-save on blur/change. Stop-loss checkbox
   (`stopLossChk`) and value (`stopLossPct`) are both in the arrays and persist correctly.

**All settings now persist across hard refresh (Ctrl+Shift+R):**
- ✓ Sell Strategy mode (resolution/profit/%/price) 
- ✓ All % profit buttons + custom value
- ✓ All $ profit buttons + custom value
- ✓ All price buttons + custom value
- ✓ Stop-loss checkbox + value
- ✓ Scanner ranges, categories, max spend, max buys
- ✓ Auto-run mode settings

**Files changed:** `app.py` (2 edits), `index.html` (1 edit). Both pass syntax checks.

---

## 2026-06-03 Session 2 — Branch consolidation + multi-bot direction

**Consolidated all branches into one clean `main`** (so a single web Claude Code at
claude.ai/code can work from PC/laptop/mobile off one repo). Reviewed all 6 branches:
- **Merged in:** my stop-loss/sell/strategy/cash fixes; the **`/mobile` phone page**
  (`mobile.html`) + 404 "market closed" handling on buy/sell (from `mobile-local-trading-bot`);
  the **ghost-position reconciliation** in `/api/portfolio` (drops tracked-"open" positions
  Kalshi no longer reports, >90s old — from the `drone-evtol` branch's one real fix).
- **`/api/debug/balance`** (pre-existing, used by mobile page) now routed through the cached
  `_get_balance()`; `/api/balance` (main page) kept. Both cached now.
- **Dropped:** the eVTOL/Skyway commits (wrong project), `hello-v68ub` (just MCP perms),
  and the **port-5003 "kalshi bot 2"** config (superseded via `git merge -s ours`).

**Kalshi = ONE bot on port 5003** (2+ bots on one Kalshi account risks ToS — user confirmed).
**Port scheme for the user's bots (updated 2026-06-04):** 5003 Kalshi (manager 5103),
5004 Binance (manager 5104), 5001 CNS bot, 5002 CNS-tree. Each is its OWN project
(don't mix — see router). NOTE: Binance claimed 5004/5104, so Kalshi moved 5004 → 5003.

**⚠️ KEY ARCHITECTURE GAP (next work):** the bot only runs *in the background* for SELLING
(the `_monitor` thread). **Scanning + buying runs in the browser tab** (JS auto-mode loop), so
closing the browser stops buying. The user wants "hit run, close browser, keeps botting" — that
requires **moving the scan/buy loop into a backend thread**. Applies to CNS/Binance bots too.

**Roadmap:** (1) ✅ clean Kalshi main; (2) move scan/buy to backend (true headless botting);
(3) build a **"Bots Home" launcher** as its OWN root project (lists Kalshi/CNS/CNS-tree/Binance,
start/stop each, status); (4) Binance bot (new project).

**Multi-device setup answer:** local Claude Code (PC) = local-only sessions; cloud
`claude.ai/code` = synced across devices but edits via GitHub only. To use THIS PC's Claude
(edits+runs the bot) from anywhere: `claude remote-control` on the PC → URL. To centralize: use
`claude.ai/code` everywhere off the clean repo. Bot must run on one always-on machine; reach its
dashboard remotely via Tailscale. Never run the bot on two machines at once (caused the dup-process
chaos: 4× `app.py` + 2 spawners → 429s + cash starvation; fixed by killing all, running one
manager+app.py).

---

## 2026-06-03 Session — Cash "—" fix (rate-limit overhaul)

**Symptom:** cash not showing at top (navCash stuck on "—"). **Root cause:** the
"AGGRESSIVE" rate limiter (`_rate_limit_delay = 2.0`, flat 2s between EVERY Kalshi call,
**no 429 retry**). `/api/portfolio` carries the cash (`balance`), but it makes many serialized
calls (balance + positions pages + 1 per open position) and competes for the same global
`_api_lock` with the scan loop (`_rate_get` → `kalshi_get`, ~30+ series probes). While a scan
runs, the balance call sits in the 2s-gapped queue past the frontend timeout, so `balance`
came back null/late → cash blanked. A single 429 also nulled it (no retry).

**Fixes (app.py + index.html):**
1. **Rate limiter rewrite** (`_kalshi_request`): base delay 2.0s → **0.5s**, plus real
   **429 backoff+retry** (1/2/4/8s, honors `Retry-After`). GET also retries timeouts; POST
   does NOT (a timed-out order may have filled — avoid double-orders; 429 retry on POST is
   safe via `client_order_id` idempotency). `kalshi_get`/`kalshi_post` signatures unchanged.
   ⚠️ This intentionally reverses the prior "2s flat" commits (bf7f2f2/f27d706/c0db9a8) —
   the 429 retry is the new safety net. If 429 spam returns, bump `_rate_limit_delay`.
2. **Decoupled cash:** `_get_balance()` (12s cache, serves last-known on failure so cash
   never re-blanks) + new lightweight **`/api/balance`** endpoint. `/api/portfolio` now uses
   the cache. Frontend `loadCash()` hits `/api/balance` on load + every 15s (visible only),
   so cash shows immediately, independent of the slow positions load. Cache invalidated on
   buy/sell (`_balance_cache["ts"] = 0`).

**BIGGEST real-world cause — duplicate processes:** found **4 concurrent `app.py` instances**
running at once, spawned by TWO different launchers both alive: `kalshi-manager.py` (Popen) AND
a `python -c "while True: subprocess.run(['python','app.py'])"` supervisor loop. Each instance
runs its own monitor thread + scan, all hitting the SAME Kalshi account; the rate-limit lock is
per-process so they don't coordinate → ~4× API load → 429s + cash starvation. Killed all of them
+ both spawners, started ONE clean `app.py`. Verified: `/api/balance` → cash $0.32, portfolio
$25.72, 35 open positions. **RULE: run only ONE launcher. Never run kalshi-manager.py AND the
supervisor AND a manual `python app.py` together.**

Both files pass syntax checks. NOT yet committed/pushed. **Was NOT caused by the 06-02 changes.**

---

## 2026-06-02 Session — Stop-loss fix + sell reliability + UI cleanup

Four fixes (restart `python app.py` + hard-refresh browser to apply):

1. **Stop-loss now actually fires.** Root cause: the monitor read stop-loss from
   `sell_strategy`, but the frontend saved it to a *different* dict, `sell_settings`
   (via `/api/sell-settings`) — they never synced, so `stop_loss_pct` was always `None`
   and the stop-loss branch never ran. Fixed: stop-loss is now stored in `sell_strategy`
   via `/api/strategy` (persisted to `bot_strategy.json`), which is what the monitor reads.
   New per-position control "🛑 Cut losses if a position drops X%" added to the Sell
   Strategy card (`stopLossChk` + `stopLossPct`). Works for any %, applies on top of the
   profit exit. Removed the dead stop-loss handling from `/api/sell-settings`.
2. **Deleted the auto-mode "Stop Loss — Auto-sell if you lose X" section** (stopProfitDol/Pct,
   stopLossDol/Pct) and its driver `checkAutoStopConditions()` — mislabeled, overlapped
   RUN UNTIL, loss-sell path broken. Also removed the stale stop-loss conflict check.
3. **Sell-strategy now persists on refresh.** `loadSettings()` + a boot-time line both
   called `setProfit()`, which force-set mode=`profit` every load and clobbered the saved
   choice. Now the saved mode is applied LAST and wins. (Removed the forced `setProfit` at boot.)
4. **"Sold then comes back a minute later" fixed.** Manual `/api/sell` was a LIMIT order;
   when it rested unfilled the code still marked it sold + hid it ~2 min, so it reappeared.
   Now it's a MARKET order with a protective floor = current bid (won't accept worse; never
   rests). If the bid moved/no buyers → honest "couldn't sell, try again" instead of a fake
   sale. Sub-1-contract leftovers (Kalshi can't sell <1) now show "resolves @ expiry" instead
   of a dud Sell button.

Files: `app.py` (`/api/strategy`, `/api/sell-settings`, `/api/sell`), `index.html`
(Sell Strategy card, Auto Mode card, `pushStrategy`, `pushSellSettings`, `loadSettings`,
`checkStrategyConflicts`, positions renderer, settings lists). Both pass syntax checks.
NOT yet committed/pushed to GitHub — do that after the user confirms behavior.

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

*Last updated: 2026-06-03*
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
