# KalshiBot Bug Log

> **READ THIS BEFORE TOUCHING balance/portfolio/price calculations.**
> Each entry: bug, root cause, fix, and what NOT to do again.

---

## BUG-001: Portfolio showing $196 instead of $15 (FIXED 3x, keep this rule)

**Symptom:** PORTFOLIO+CASH shows wildly wrong number (e.g. $196 when Kalshi shows $15)

**Root Cause — THE `> 200` HEURISTIC IS WRONG:**
```python
# THIS IS WRONG — DO NOT USE THIS PATTERN:
pos_dollars = pos_raw / 100 if pos_raw > 200 else pos_raw
```
When `portfolio_value` from Kalshi = 183 cents ($1.83), since 183 < 200 it treats $1.83 as $183.

**Kalshi API field types — NEVER CHANGE THESE ASSUMPTIONS:**
- `balance_dollars` → always a dollar string like `"13.60"` (divide by nothing)
- `balance` → always in CENTS (divide by 100)
- `portfolio_value` → always in CENTS (ALWAYS divide by 100, no heuristic)

**Correct calculation:**
```python
total_dollars = float(bal_data.get("balance_dollars") or 0)  # dollars
pos_cents     = float(bal_data.get("portfolio_value") or 0)  # cents
pos_dollars   = round(pos_cents / 100, 2)                    # ALWAYS /100
balance       = round(total_dollars - pos_dollars, 2)        # cash
total_account = round(total_dollars, 2)                      # matches Kalshi
```

**Fixed in:** `app.py` → `/api/portfolio` balance section
**Broke again because:** Someone re-introduced the `> 200` heuristic trying to "be safe"
**Rule:** `portfolio_value` is ALWAYS cents. `balance_dollars` is ALWAYS dollars. No heuristics.

---

## BUG-002: Market cache causing blank positions (FIXED)

**Symptom:** VALUE NOW, PROFIT NOW, TIME LEFT all showing `—` in positions table

**Root Cause:** `_get_market()` had a recursive call bug:
```python
def _get_market(ticker):
    data = _get_market(ticker)  # <-- called itself! infinite recursion
```

**Fix:** Changed to `kalshi_get(f"/markets/{ticker}").get("market", {})`

**Also added:** Don't cache empty/failed responses. Cache failures for 5 min to stop spam.

---

## BUG-003: Stats stuck on "Loading..." (FIXED)

**Symptom:** Stats tab shows "Loading..." forever

**Root Cause:** Kalshi settlements API times out (~30+ seconds), no timeout on fetch

**Fix:** Added 15s AbortController timeout on frontend fetch. Shows "Kalshi API slow, try again" on timeout.

---

## BUG-004: Sell button showing "..." twice before selling (FIXED)

**Symptom:** Click Sell → "..." → position reappears → click again → "..." → finally gone

**Root Cause:** After sell succeeds, `loadBalance()` immediately refetches positions from Kalshi API which has a brief delay, so the position comes back.

**Fix:** Immediately remove position from `allPositions` in memory on successful sell. Delay balance refresh 3 seconds. If sell FAILS, restore position via immediate `loadBalance()`.

---

## BUG-005: Bot kept stopping in "Until stopped" mode (FIXED)

**Symptom:** Bot stops buying even with "Until stopped" selected

**Root Cause 1:** Cash balance showed negative (-$1.92) due to BUG-001, triggering cash check stop.
**Root Cause 2:** `navPortfolio.textContent` read as "—" during loading → parseFloat("—") = NaN → 0 < buyAmount → stop triggered.

**Fix:** Only check cash if value has loaded (not "—"). In "Until stopped" mode, never fully stop on cash issues — instead pause 1 min and retry with countdown.

---

## BUG-006: Tab nesting (tabs inside scanner tab) (FIXED)

**Symptom:** Positions, Stats, Coach tabs showed blank content

**Root Cause:** Browser HTML parser nested tab-pane divs inside tab-scanner due to unclosed tags or parsing quirk.

**Fix:** JavaScript on init moves all `.tab-pane` elements to be direct children of `.main`:
```javascript
document.querySelectorAll(".tab-pane").forEach(p => {
  if (p.parentElement !== main) main.appendChild(p);
});
```

---

## BUG-007: Monitor auto-sell 400 error (FIXED)

**Symptom:** `[API 400] POST /portfolio/orders → invalid_order: exactly one of yes_price, no_price...`

**Root Cause:** Monitor used old market-order format without price field. Kalshi API now requires a price.

**Fix:** Fetch current bid before selling, include `yes_price_dollars` or `no_price_dollars` in order. Skip if bid < 1¢ (no buyers).

---

## BUG-008: "tracked fallback" log spam (FIXED)

**Symptom:** CMD spammed with `[portfolio] tracked fallback used for KXBTC-...` every few seconds

**Root Cause:** Expired/resolved markets kept retrying market data fetch on every portfolio call

**Fix:** Cache failed market lookups for 5 minutes. Suppress the print statement.

---

## RULES TO NEVER BREAK

1. **`portfolio_value` from Kalshi balance API = ALWAYS cents → always divide by 100**
2. **`balance_dollars` from Kalshi balance API = ALWAYS dollars → use directly**
3. **Never use `> 200` heuristic for Kalshi API field type detection**
4. **Market sell orders MUST include `yes_price_dollars` or `no_price_dollars`**
5. **The `_get_market()` cache function must call `kalshi_get()`, NOT itself**
6. **"Until stopped" auto mode should NEVER fully stop due to cash — pause instead**
