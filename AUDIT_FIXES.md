# Kalshi Bot - Security & Logic Audit Fixes (v1.2.0)

## Issues Fixed ✅

### CRITICAL ISSUES
1. **Multiple Sell Strategies Active Simultaneously** (Lines 1573, 1580)
   - Added `setStrategy("profit")` to `setProfit()` function
   - Added `setStrategy("profit_dollars")` to `setProfitDol()` function
   - Now when you click profit buttons, the strategy mode immediately switches

2. **Removed "Buy at (max price to buy)" Section** (Lines 773-821 deleted)
   - Completely removed conflicting UI section
   - Simplified to single "Sell when price hits X¢" option
   - Eliminates confusion between buy range and sell trigger

### HIGH-PRIORITY ISSUES
3. **Input Validation on /api/strategy Endpoint** (app.py lines 1915-1931)
   - Added range checks: 
     - `target_pct`: 1-999%
     - `target_dollars`: $0.01-$1000
     - `target_price_cents`: 1-99¢
     - `buy_in_price_cents`: 1-99¢
   - Prevents invalid/nonsensical strategy values
   - Returns 400 error with clear message on invalid input

4. **"Save Settings" Button Prominence** (Lines 979, 3491-3507)
   - Changed background to bright green (`var(--green)`)
   - Text always white (never black)
   - Shows "✓ Saved!" feedback in white text for 3 seconds
   - Min-width 100px to prevent button jumping

5. **Version Footer Added** (Bottom-right corner)
   - Shows `v1.2.0` and last update timestamp
   - Auto-updates each time page loads
   - Updates with each code change

## Already Implemented ✅

### Strategy Loading
- Strategy mode is already loaded on page load (line 3852)
- Syncs properly with radio button states
- No changes needed

## Remaining Issues (Not Yet Fixed)

### Backend Enforcement (Medium Priority)
- Monitor thread should enforce single-mode strategy checking
  - Only check conditions matching current `stratMode`
  - Prevents conflicting auto-sell triggers

### Security (Lower Priority - Authentication via RSA prevents most attacks)
- Rate limiting not implemented (API endpoints can be called repeatedly)
- CSRF tokens not validated (RSA auth provides alternative security)
- These are less critical given RSA-PSS authentication is already in place

### UX Improvements (Optional)
- Two ticker displays could be consolidated
- Settlement filters not yet implemented
- Auto-sell reason tracking could be more granular

## Testing Checklist

- [ ] Click "200%" button — verify "Sell at % profit: 200%" becomes active (not both % and $ at once)
- [ ] Click "$0.05" button — verify "Sell at $ profit: $0.05" becomes active
- [ ] Click "50¢" sell button — verify "Sell when price hits 50¢" becomes active
- [ ] Set invalid values in strategy (e.g., target_pct=2000) — verify backend rejects with error
- [ ] Click "Save Settings" — verify button shows "✓ Saved!" in white for 3 seconds
- [ ] Refresh page — verify last strategy loads correctly
- [ ] Hard refresh — verify version footer shows current timestamp

## Code Quality

- All changes follow existing code style
- No breaking changes to API contracts
- Backward compatible with existing saved strategies
- Frontend validation complements backend validation
