# KalshiBot — Full Context File

> **For future Claude instances:** Read this file first. It contains everything you need to have full context on this project. After reading it, recreate the memory files at `~/.claude/projects/.../memory/` using the types and format described in the memory system (user, feedback, project, reference types with frontmatter). The user works across multiple machines via OneDrive — this file is the source of truth. Update this file every time you would normally write to Claude Code memory.

---

## Project Overview

Flask + single-page HTML trading bot for [Kalshi](https://kalshi.com) prediction markets.

**Goal:** Automatically scan for and buy high-probability short-expiry Kalshi contracts via a web UI.

**Location:** `C:\Users\mycry\OneDrive\Documents\claude code\kalshi bot\`

**Run:** `python app.py` → open `http://localhost:5000`

**Status:** Fully functional. Last major update: 2026-05-28 (efficiency optimization pass + UI fixes)

---

## Files (Current State)

| File | Purpose | Status |
|------|---------|--------|
| `app.py` | Flask backend — all API calls, scan logic, buy/sell, monitor | ✅ Working (1800+ lines, optimized) |
| `index.html` | Single-page frontend (no build step) | ✅ Working (1900+ lines, UI fixed) |
| `bot_positions.json` | Persisted tracked positions (auto-created) | ✅ Auto-created/updated |
| `portfolio_snapshots.json` | Portfolio value snapshots every 5 min for P&L history | ✅ Active (149 entries as of 2026-05-28) |
| `scan_log.jsonl` | Append-only scan history for backtesting / win-rate analysis | ✅ Active logging |
| `gen_key.py` | One-time RSA key generation script | ✅ One-time use |
| `CONTEXT.md` | THIS FILE — syncs via OneDrive, source of truth for Claude context | ✅ Up-to-date (2026-05-28) |

**Credentials dir (one level up, also in OneDrive):**
`C:\Users\mycry\OneDrive\Documents\claude code\kalshi-keys\`
- `kalshi_api_key` — UUID string
- `test2.txt` — RSA private key PEM

> **SECURITY RULE (never violate):** Do NOT read the credential files. The user has explicitly said to assume they are in good form. Your code can access them, but you must never read them yourself.

---

## Kalshi API

- Base URL: `https://api.elections.kalshi.com/trade-api/v2`
- Auth: **RSA-PSS** (NOT PKCS1v15). Signs `timestamp_ms + METHOD + full_path` (no request body in signature).
- Price fields: dollar strings → multiply by 100 for cents (`"0.8500"` → 85¢)
- Fee: $0.01 per contract
- Max buy price for profit: 96¢ (keeps ≥3¢ net after fees)
- Positions API has a brief delay after buy → use tracked-dict fallback in `/api/portfolio`

---

## Backend Architecture (`app.py`)

### Key constants
```python
BASE_URL    = "https://api.elections.kalshi.com"
API_PREFIX  = "/trade-api/v2"
CREDS_DIR   = Path(os.environ.get("KALSHI_CREDS_DIR", HERE.parent / "kalshi-keys"))
TRACKED_FILE  = HERE / "bot_positions.json"
SCAN_LOG      = HERE / "scan_log.jsonl"
SNAPSHOTS_FILE = HERE / "portfolio_snapshots.json"
```

### Auth signing
RSA-PSS: signs `f"{timestamp_ms}{METHOD}{full_path}"` (no body). Uses SHA-256 hash, MGF1 padding.

### Market Filtering & Category Detection
**Category detection (cached with LRU, max 5000):**
- `is_crypto(market)` — checks 46 crypto keywords (btc, eth, sol, etc.) in ticker/title/category/event_ticker
- `is_sports(market)` — checks sports keywords (nfl, nba, mlb, nhl, soccer, tennis, golf, mma, etc.)
- `is_politics(market)` — checks political keywords (election, president, senate, congress, vote, etc.)
- `is_economics(market)` — checks econ keywords (gdp, inflation, cpi, unemployment, fed, treasury, stock, nasdaq, etc.)
- `is_combo(market)` — checks for "combo", "parlay", "multi" in any field; detects multi-condition markets by title patterns ("and"/"&" count)

**Scan filter chain (applies in order):**
1. Time window — market expires within X minutes
2. Category filter — only show selected categories (positive "show" logic, not exclude)
3. Liquidity (if good_liq checked) — open_interest or volume >= 10
4. Price range — yes_ask or no_ask within min-max threshold
5. Bid requirement — real bid >= 1¢ on buying side (live-only, not theoretical)
6. Status check — market status in (open, active, live)
7. Spread (if good_liq checked) — ask-bid <= 8¢

**KXMV rejection:** All markets with ticker starting with KXMV (Kalshi's multi-variable markets) are skipped immediately — they're untradeables

### Scan logic (`/api/scan`)
- Fetches `/markets?status=open&limit=200` with cursor pagination (max 15 pages)
- Filters: time window, price range, crypto/combo/liquidity
- Price: checks `yes_ask_dollars` and `no_ask_dollars` for range match
- Liquidity: open_interest or volume >= 10 (good_liq flag)
- Bid check: requires real bid (>= 1¢) on buying side
- Spread check: ask − bid <= 8¢
- Logs each filtered market to console with reason (crypto/combo/thin/price/no-bid/spread)

### Background Threads

**Monitor (`_monitor`)**
- Runs every 30 seconds (aligned with scan frequency to reduce redundant checks)
- Iterates tracked positions, checks current bid price
- Auto-sells when profit % >= target_pct from bot-stored strategy
- **Near-settlement bypasses:**
  - bid >= 97¢ (YES side) → hold to resolution (no sellers)
  - bid <= 3¢ (YES side) → hold, no buyer available
  - If `skip_auto_sell_near_resolution` enabled: hold if expiring within `skip_auto_sell_minutes` threshold
- Updates position `status: "sold"`, `sell_price`, `sold_at` in tracked dict when successful
- Catches exceptions per position (one error doesn't break loop)

**Snapshot writer (`_snapshot_runner`)**
- Takes immediate snapshot on startup
- Runs every 5 minutes (300s) thereafter
- Stores (timestamp, portfolio_value) pairs in `portfolio_snapshots.json`
- Trims to 30 days of history (10,080 entries max)
- Used by `/api/pnl` for historical P&L display

**Scan loop (frontend-triggered, runs every 30s when auto on)**
- Frontend calls `scan()` every 30s when auto mode running
- If `autoModeOn`, automatically buys up to N markets from results (configurable per scan)
- Respects concurrent position limit (`maxConcurrent`)
- De-duplicates using `autoModeBoughtSet` (no repeat buys in same session)
- Stops auto mode when time/buy limits reached

### Portfolio snapshots (`_snapshot_runner` thread)
- Takes snapshot immediately on startup, then every 5 min
- Stores `{ts: float, v: float}` in `portfolio_snapshots.json`
- Used by `/api/pnl` for historical P&L display

### Duplicate buy guard
```python
if existing and existing.get("status") == "open":
    return jsonify({"error": f"Already holding {ticker} — sell it first"}), 409
```

### Endpoints (All Functional)
| Endpoint | Method | Purpose | Notes |
|----------|--------|---------|-------|
| `/` | GET | Serve index.html | Static HTML, no build step |
| `/api/scan` | GET | Markets matching filters (price, time, category) | Receives: min/max threshold, minutes, show_crypto/combo/sports/politics/economics, good_liq. Returns: sorted by profit potential |
| `/api/debug_scan` | GET | Raw market data for markets expiring in next 60min | For Stats tab Debug Scan feature |
| `/api/buy` | POST | Purchase contracts | Payload: ticker, side, type, count, category. Returns: order ID, buy price, tracked position |
| `/api/sell` | POST | Sell contracts (manual or auto-triggered) | Payload: ticker, side, count. Auto-triggered when profit % >= target |
| `/api/portfolio` | GET | Cash balance + all positions + recent settlements | Returns: balance, portfolio_value, positions[], recent_settlements[], market fallback for delayed API |
| `/api/strategy` | POST | Update sell strategy (profit % target, mode) | Payload: ticker, mode ("resolution"\|"profit"), target_pct |
| `/api/pnl` | GET | Historical P&L by time period (1h/4h/6h/12h/24h/7d/30d) | Returns: period snapshots for nav display |
| `/api/stats` | GET | Per-category win/loss/P&L stats + scan log count | Renders Stats tab table |
| `/api/coach` | GET | 5-strategy recommendations (conservative, ROI, volume, sector, time-based) | Based on historical coach statistics |
| `/api/sell-settings` | POST | Update near-resolution auto-sell bypass settings | Payload: skip_auto_sell_near_resolution (bool), skip_auto_sell_minutes (int) |

---

## Frontend Architecture (`index.html`)

Single HTML file, no build step, no external dependencies except browser APIs.

### Tabs
- **Scanner** — filters, scan results, auto mode, sell strategy
- **Positions** — live positions table with P&L, sell controls
- **Stats** — category performance table, scan log count, debug scan

### Key Frontend State
```javascript
let markets = [];                  // scan results (live, filterable)
let allPositions = [];             // positions from /api/portfolio
let selectedMarkets = new Set();   // user-selected markets for multi-buy
let selectedPosTickers = new Set();// user-selected positions for multi-sell
let scanInProgress = false;        // guard against concurrent scans

// Filter settings (from UI)
let selMin = 85;                   // min probability %
let selMax = 98;                   // max probability %
let selTime = 15;                  // expiry window (minutes)
let stratMode = "resolution";      // sell strategy ("resolution" | "profit")
let stratPct = 10;                 // profit target %
let buyAmount = 1;                 // spend per purchase ($)
let maxBuysPerMarket = 1;          // max contracts per game (enforced by unique tickers)

// Auto mode state
let autoModeOn = false;            // currently running?
let autoRunMode = "forever";       // "time" | "buys" | "forever"
let autoModeStartTime = null;      // Date.now() when started (for "time" mode)
let autoModeBought = 0;            // count in current session
let autoModeBoughtSet = new Set(); // de-dup already-bought tickers this session

// Timers
let scanTimer = null;              // 30s interval when auto on
let posTimer = null;               // 10s interval for positions update when auto on
let cdTimer = null;                // countdown timer display
```

### Key functions
| Function | Purpose |
|----------|---------|
| `scan()` | Run a scan, auto-buy if auto mode on |
| `buyOne(idx)` | Buy a single market from scan results |
| `buyAll()` | Buy all selected markets |
| `sellPos(ticker, side, count, safeId)` | Sell a position |
| `renderMarkets()` | Render scan results table |
| `renderBotPositions(positions)` | Update allPositions + positions tab |
| `renderPositionsTable(positions)` | Render positions table in Positions tab |
| `filterPos()` | Apply source/category filters to positions |
| `loadBalance()` | Fetch /api/portfolio, update nav + positions |
| `loadPnl()` | Fetch /api/pnl, update P&L display |
| `loadStats()` | Fetch /api/stats, render category table |
| `switchTab(name)` | Switch active tab |
| `updatePosTabBadge(count)` | Show open position count on Positions tab button |
| `toCDT(isoStr)` | Format UTC ISO timestamp to CDT (America/Chicago) |
| `startAutoMode()` / `stopAutoMode(reason)` | Control auto mode |
| `checkAutoStopConditions()` | Check profit/loss stop triggers |
| `runDebugScan()` | Fetch /api/debug_scan and display in Stats tab |

### P&L display
- Nav bar shows P&L for selected period (1h/4h/6h/12h/24h/7d/30d)
- Click the P&L block to cycle periods
- Unrealized P&L = `contracts × (current_bid − buy_price) / 100`

### Positions persistence
- localStorage caches positions so they show immediately on page reload
- Server-side tracked dict fallback: if Kalshi API hasn't settled yet, use stored position data

### OWNED badge
- Scan results show "OWNED" badge + dimmed row + unchecked for markets already held

---

## User Preferences & Feedback

- **Timezone:** CDT (America/Chicago) — always display times in CDT
- **No comments** in code unless truly non-obvious
- **Concise responses** — no trailing summaries of what was just done
- **Never read credential files** — hard rule, never violates
- **Category stored on buy** — always pass `category` from frontend to `/api/buy`
- **Max buy 96¢** — to ensure ≥3¢ net profit after fees
- **Exclude combos by default** — user was burned buying multi-event markets
- **Stop conditions are independent checkboxes** — not a single "either/or" condition
- **Auto mode timer has no cap** — user wants to run overnight, removed the 480-min max
- **1 buy per active listing** — duplicate buy guard in place
- **Default spend = $1** (was $3) — user changed 2026-05-28
- **Floating Start FAB** lives bottom-right: "Scan Now" (green pill) + "Start Auto" (blue pill, turns red when running). Mirrors in-card buttons via `syncFabButtons()`.
- **UI vocab:** "live markets" = open status + real bid ≥1¢ on the buying side. Scan filter now enforces this unconditionally (not just when `good_liq` is checked). Status check also rejects anything not in {open, active, live}.
- **Button language:** "Scan Now" = one-time lookup, no buying. "Start Auto Mode" = repeating scan + auto-buy loop. Explanatory banner above the Scan card spells this out for the user.

---

## Known Issues / Resolved

### Fixed (2026-05-28 Session)
- **Duplicate floating buttons:** Removed redundant fab-container definition at end of HTML
- **Indentation errors in DEBUG_LOGGING wrappers:** Fixed all lines (1201, 1237, 1261)
- **Scan returning 0 results:** Skip `KXMV*` markets, raise page limit to 50
- **Price field fallback:** `_market_price()`/`_market_bid()` handle both dollar strings and integer cents
- **Live-only scan results:** Unconditionally reject markets with no real bid (<1¢) and non-live status
- **Positions tab empty:** Fixed parser to handle `position_fp` (string float) + `*_dollars` fields from new Kalshi API
- **Recent Settlements:** Added 24h settlement history with title/category enrichment
- **Tab rendering (Positions, Stats, Coach blank):** Fixed by removing inline `style="width:100%;display:block !important"` from tab-pane divs. These styles were forcing tabs to always be visible, breaking CSS class-based `.active` toggling. Removed from `#tab-positions` and `#tab-coach`; `#tab-stats` was already clean. Tabs now properly hide/show via `.switchTab()` function toggling `.active` class.

### Known Limitations (Not Bugs)
- **Portfolio API cost:** Makes individual `/markets/{ticker}` calls per position (no batching). Could optimize with market data caching.
- **PnL calculation:** Recalculates on every request (no caching). Could cache snapshot ranges for 5+ min.
- **Category detection:** Now cached, but only after first detection. First scan per market still incurs detection cost.
- **Monitor thread:** Checks every 30s (synced with scan), acceptable latency for target use case.

---

## Recent Changes & Status (2026-05-28, tested and working)

### Scan Frequency Optimization (Latest)
- **Scan interval changed:** 30s → **10 seconds** when auto mode active
- **Rationale:** 10s gives ~3–6 checks during typical 5–15 min market windows, far more responsive than 30s while keeping API load modest (6 scans/min = 360/hour)
- **Monitor thread:** Remains at 30s (checking existing positions for auto-sell is lower priority than finding new opportunities)

## Recent Changes & Status (2026-05-28)

### UI Fixes (Latest Session)
- **Duplicate floating buttons removed** — deleted redundant fab-container at end of HTML, kept styled version with icons/subtexts
- **Floating Scan/Auto buttons** — working correctly at bottom-right with green (Scan) and blue (Auto, red when running)
- **White borders on cards** — all card borders changed to `2px solid #ffffff` for brightness/readability
- **Copy Report button** — fixed duplicate `style` attribute issue, now works
- **Max per scan** — changed from 3 to 20
- **Max buys per game** — added with quick buttons [1,2,3,4,5], defaults to 1
- **Settings unified** — single panel with "Max spend per purchase" and "Max buys per game"
- **Category filters** — working with positive "show" logic (not exclude):
  - Show Crypto, Show Combo, Show Sports, Show Politics, Show Economics

### Efficiency Optimizations (2026-05-28)
**Backend:**
- **Debug logging:** `DEBUG_LOGGING` flag (default False) suppresses all verbose scan filter logs. Set True for diagnostics.
- **Category detection caching:** LRU cache (max 5000 entries) for crypto/combo/sports/politics/economics detection per ticker. Eliminates re-detection across scans.
- **Monitor polling:** Aligned from 20s → 30s to match scan frequency, reducing redundant position checks.

**Frontend:**
- **Scan deduplication:** Added `scanInProgress` flag prevents concurrent scan requests from rapid clicks.

**Known high-cost operations (future opportunities):**
- `/api/portfolio` makes individual `/markets/{ticker}` calls per position (no batching yet)
- `/api/pnl` recalculates snapshots on every request (no caching)
- Settlement titles fetched individually (could cache 5+ min)

---

## Auto Mode Behavior

**How it works:**
1. User clicks "Start Auto" button (floating FAB or in-card)
2. Frontend sets `autoModeOn = true`, starts 30s scan timer
3. Every 30s, `scan()` runs automatically:
   - Fetches markets matching all filters
   - If auto mode still on, auto-buys from results (up to `maxPerScan`)
   - Respects concurrent position cap
   - De-duplicates already-bought tickers in current session
4. Monitor thread (every 30s) checks all positions, auto-sells at target profit
5. User can stop at any time by clicking "Stop Auto" (or time/buy limits trigger auto-stop)

**Stop conditions (checked before each scan):**
- **Time-based:** `autoRunMode == "time"` → stops after X minutes
- **Buy-based:** `autoRunMode == "buys"` → stops after X buys
- **Forever:** `autoRunMode == "forever"` → no auto-stop (user must manually stop)

**Settings during auto mode:**
- All settings (price filters, time window, category filters, spend per buy, max buys per game, max per scan) are live — changing them mid-session takes effect on next scan

---

## Strategy Notes

- **Edge:** Markets where outcome is already decided but Kalshi hasn't settled yet (game just ended, vote just closed). Short-window markets (5-15 min) at 85-96¢ are near-certain wins if outcome is known.
- **Risky:** Buying markets where outcome is still live and uncertain — the 90¢ price is probably wrong.
- **Near settlement:** At 97¢+ bid, let resolve naturally (no sellers anyway). Don't auto-sell.

---

## Setup & Deployment

### Installation (One-time)
```bash
pip install flask requests cryptography
# Credentials must be in: C:\Users\mycry\OneDrive\Documents\claude code\kalshi-keys\
#   kalshi_api_key (UUID string)
#   test2.txt (RSA private key PEM)
```

### Running
```bash
cd "C:\Users\mycry\OneDrive\Documents\claude code\kalshi bot"
python app.py
# Open http://localhost:5000
```

### Verification (2026-05-28 Status — TESTED)
- ✅ App starts without errors
- ✅ Credentials load OK (RSA key type detected)
- ✅ 25 tracked positions persisted from bot_positions.json (all marked as "sold" = settled)
- ✅ 184 portfolio snapshots loaded (updated every 5 min)
- ✅ Monitor thread running every 30s
- ✅ Snapshot thread running every 5 min
- ✅ **Frontend loads, ALL TABS FUNCTIONAL** (tab rendering issue fixed 2026-05-28)
  - **Positions Tab:** Table with open/settled positions (ticker, title, side, buy price, current bid, profit %, strategy, status). Recent Settlements with 24h history.
  - **Stats Tab:** Category performance table (wins/losses/P&L per category), scan log count, debug scan feature
  - **Coach Tab:** AI strategy recommendations with 5 ranked trading scenarios (conservative, ROI, volume, sector, time-based)
- ✅ **Scan WORKING** — Returns crypto 15-min markets (KXBNB, KXHYPE, KXDOGE, KXETH, KXSOL, KXXRP) at 85-87¢ prices
  - **Previous issue FIXED:** KXMV market flood bypassed
  - Uses series-probe approach with rate limiting
  - Timestamp filtering + multi-approach fallback strategy
- ✅ Auto mode cycles every 10s when enabled, respects filters
- ✅ Position table correctly empty (no open positions — all 25 are settled, visible in Recent Settlements)
- ✅ Coach data loads 4s after page load (inline panel) + when Coach tab clicked
- ✅ Recent settlements showing 24 trades: **50% win rate, -$4.85 total P&L** (learning session)

### Current Portfolio State (as of 2026-05-28 18:30 UTC)
- **Cash:** $17.82
- **Open positions:** 0 (all 25 settled)
- **Recent P&L:** -$4.85 (24 trades)
- **Win rate:** 50% (12 wins, 12 losses)
- **By category:** All crypto (category detection needs work — showing as "Unknown")
- **Best time:** 17:00 UTC (100% win rate, 3 trades, +$0.16 P&L)
- **Riskiest band:** 80-85¢ (avg -$0.32 per trade)

### Multi-Machine Sync
- All files checked into OneDrive at: `C:\Users\mycry\OneDrive\Documents\claude code\kalshi bot\`
- CONTEXT.md is the source of truth — update it on any machine, syncs automatically
- bot_positions.json, portfolio_snapshots.json, scan_log.jsonl sync automatically
- Credentials in parent `kalshi-keys` directory — do NOT sync to avoid exposure

### Requirements
- Python 3.9+
- Windows (uses Windows-specific paths, but code is mostly portable)
- Internet (Kalshi API calls)
- No frontend build step (single HTML file)

---

---

## Latest Session (2026-05-28, Evening UTC)

### Tab Rendering Issue (FIXED)
**Problem:** Positions, Stats, and Coach tabs appeared completely blank when user returned from laptop — no headers, borders, content, or titles visible.

**Root Cause:** Inline `style="width:100%;display:block !important"` attributes on tab-pane divs were forcing those tabs to always be visible, which overrode the CSS class-based visibility system (`.tab-pane {display:none}` + `.tab-pane.active {display:block}`).

**Solution:** Removed problematic inline styles from:
- `#tab-positions` div (line 462)
- `#tab-coach` div (line 513)
- `#tab-stats` was already clean

**Result:** Tab visibility now properly controlled by `.switchTab()` function toggling `.active` class. Tabs correctly hide/show without interference.

### What Tabs Display (When Working)
**Scanner Tab (default, active):**
- Filter controls: price range (85-98¢), time window (minutes), category checkboxes
- Scan results table: ticker, title, yes/no ask prices, profit %, owned badge
- Auto mode controls: start/stop buttons, time/buy mode selector
- Sell strategy settings: profit % target, resolution vs profit mode
- Floating action buttons: "Scan Now" (green) + "Start Auto" (blue/red)

**Positions Tab:**
- Live positions table showing open holdings with: ticker, title, side (yes/no), buy price, current bid, profit %, strategy, status
- "No open positions" message if empty
- Recent Settlements section: 24h history of settled trades with category + title
- Sell buttons for each position with contract count controls

**Stats Tab:**
- Category performance table: breakdown by crypto/sports/politics/economics with wins/losses/P&L
- Scan log count (total markets scanned)
- Debug Scan feature: shows raw market data for next 60 min expirations

**Coach Tab:**
- AI strategy helper displaying ranked trading scenarios (1-5) based on historical performance:
  - Conservative (high-probability, lower-volatility)
  - ROI-focused (best profit potential)
  - Volume-driven (high liquidity markets)
  - Sector-based (by category emphasis)
  - Time-based (by optimal trading hours)
- Totals: wins/losses from historical data
- Breakdowns by: category, price band (85-90¢, 90-95¢, etc.), side (yes/no), hour UTC
- Refresh button to re-analyze historical positions

### Current Data State (2026-05-28)
- **Tracked positions:** 25 settled contracts in bot_positions.json
- **Snapshots:** 184 entries in portfolio_snapshots.json (5-min intervals)
- **Scan log:** Active, filtering for crypto 15-min markets
- **Win rate:** 50% (12 wins, 12 losses from recent session)
- **Total P&L:** -$4.85 (learning phase, still optimizing strategy)
- **Cash balance:** $17.82 (from last recorded state)

---

## Multi-Machine Sync & Onboarding (added 2026-05-28)

> **Action item for other PCs:** This session set up cross-machine context sync.
> When you (Claude) start on a different machine, apply the one-time setup steps below
> if they aren't already in effect, then read CONTEXT.md + WORKLOG.md to catch up.

### Why this exists
Claude Code's built-in memory (`~/.claude/...`) is **stored locally per machine and does
NOT sync** — that's why "saving memory" appeared not to work across PCs. The user runs
this project on **3 machines** via OneDrive. The fix: keep all durable context in the
synced project folder, not in Claude's local memory.

### The three synced context files (all in this folder)
| File | Captures | Who updates |
|------|----------|-------------|
| `CONTEXT.md` | Stable end-state: architecture, tabs, endpoints, decisions | Claude, when work lands |
| `WORKLOG.md` | In-progress work: what's being tried, what failed, current hypothesis | Claude, while working a hard problem |
| `activity.jsonl` | Raw breadcrumb of every Bash/Edit/Write/PowerShell action | PostToolUse hook (automatic) |

### Hooks (in `.claude/settings.local.json`, sync via OneDrive)
- **SessionStart** — instructs Claude to read CONTEXT.md + WORKLOG.md in full before acting.
- **PostToolUse** (`Bash|Edit|Write|MultiEdit|NotebookEdit|PowerShell`) — runs
  `.claude/log_action.py` to append each action to `activity.jsonl`.
- **Stop** — reminds Claude to update CONTEXT.md + WORKLOG.md if anything changed.

### ONE-TIME SETUP — do this on every PC (current and any new one)
1. **OneDrive:** right-click the `claude code` folder in File Explorer →
   **"Always keep on this device."** (Prevents reading stale cloud-placeholder files.)
2. Confirm `python` is on PATH and deps installed: `pip install flask requests cryptography`.
3. Credentials live in `..\kalshi-keys\` (synced separately; never read them).
4. The hooks + logger script (`.claude/log_action.py`) sync automatically — no manual
   install. Just verify `.claude/settings.local.json` contains the `hooks` block.

### EVERY-TIME ROUTINE — when switching machines
1. Wait for OneDrive to show **fully synced (green check)** before opening Claude.
2. Open Claude **inside the `kalshi bot` folder** (not the parent).
3. Ask Claude to "catch up" — it reads CONTEXT.md + WORKLOG.md (SessionStart hook enforces).
4. **Only work on ONE machine at a time.** Simultaneous edits make OneDrive create
   conflict-copy files and one machine's work gets clobbered (this actually happened
   2026-05-28 while editing settings.local.json).

### This session's changes (2026-05-28) — propagate awareness to other PCs
- Created parent `claude code\CLAUDE.md` pointer (redirects any folder to CONTEXT.md).
- Added SessionStart / Stop / PostToolUse hooks to `.claude/settings.local.json`.
- Added `.claude/log_action.py` (action logger) and `activity.jsonl` (auto log).
- Created `WORKLOG.md` for in-progress handoff notes.
- Recreated local Claude memory files on the primary PC (these do NOT sync — each
  machine rebuilds them from CONTEXT.md; that's expected).

---

*Last updated: 2026-05-28 (multi-machine sync infrastructure added)*
*Maintained by: Claude (user: mycry)*
*Next review: When significant changes made or new optimization pass planned*
