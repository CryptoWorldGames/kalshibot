# WORKLOG — In-Progress Work / Handoff Notes

> **Purpose:** Captures *active* problem-solving across machines — what's being attempted
> right now, what was tried, what failed, current hypothesis. This is the "mid-problem"
> handoff that `CONTEXT.md` (stable end-state) does not capture.
>
> **For Claude:** While working a non-trivial problem, append entries here as you go —
> what you tried, the result, and what's next. Newest entry at top. When a problem is
> fully resolved, fold the outcome into `CONTEXT.md` and trim the resolved WORKLOG entry.
>
> **Companion file:** `activity.jsonl` — auto-appended log of every Bash/Edit/Write
> action (timestamp + tool + detail), written by the PostToolUse hook. That's the raw
> breadcrumb trail; this file is the human-readable narrative.

---

## Active

**Feature request: Add "All" or "Max" button to Ends Within**
- User wants broad market capture but 24h is the highest preset button
- Currently: 5min, 10min, 15min, 20min, 30min, 1h, 2h, 3h, 4h, 6h, 12h, 24h, Custom
- Add button: "All (365d)" that sets to 525600 minutes
- Prevents users from typing huge custom values that could overflow
- UI: Add after 24h button or after Custom button

---

## Recently Resolved

- 2026-05-30 — Fixed scan endpoint timedelta overflow. Frontend can pass huge `minutes` values (e.g. 99999999999) which overflow Python's timedelta. Added input validation (cap at 525600 min = 1 year) + try/except safety net. Also documented in BUGLOG-010.

- 2026-05-28 — Set up cross-machine sync infrastructure: parent CLAUDE.md pointer,
  SessionStart/Stop hooks, PostToolUse action logging (activity.jsonl), and this WORKLOG.
  Documented full onboarding + every-switch routine in CONTEXT.md ("Multi-Machine Sync
  & Onboarding" section). Any new PC: read CONTEXT.md and follow the ONE-TIME SETUP there.

**Sell strategy buttons need checkmark visual indicator**
- User can't see which strategy is selected (buttons highlight green but no checkmark icon)
- Need to add ✓ symbol next to active sell strategy button/amount
- Currently: button is green but no visual confirmation
- Fix: Add checkmark text (✓) or icon to active strategy button so user knows it's saved


---

## RESTORE POINT: 6:22 PM CDT May 30, 2026

**Timestamp:** 2026-05-30 18:22:00 CDT

**Current State (Good):**
- Sell endpoint fixed: supports fractional quantities (>= 0.001)
- Links fixed: using kalshi_url from API instead of hardcoded
- Backend sleep optimized: 0.15s → 0.01s per position
- Page load still slow (30-60s reported despite optimizations)

**Files Changed:**
- app.py: /api/sell endpoint (count as float, validation >= 0.001)
- app.py: _kalshi_url() function (event_ticker format)
- app.py: /api/portfolio (sleep 0.15s → 0.01s)
- index.html: market links use kalshi_url field
- index.html: responsive layout (100% width)

**Next:** Implementing progressive loading strategy
- Phase 1 (0-2s): positions + balance (no market data)
- Phase 2 (2-7s): market enrichment
- Phase 3 (7+s): settlements/stats (background)

If breaking: revert to this point.

---

