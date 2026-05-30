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

**2026-05-30 — Bot not buying despite running auto mode**
- Symptom: Scan runs (10s intervals), finds 0 results, no buys
- Logs show: `[scan] flat pagination fallback` repeatedly
- Settings: min_thr=50, max_thr=98, minutes=1440 (24h), good_liq=true, crypto enabled
- Root cause: 1440min window too broad. Crypto 15M markets only exist in 15-min slots (e.g. :00, :15, :30, :45 marks). At arbitrary time like 1:52pm, no 15M markets are "live" closing in next 24h — they're future windows or already expired.
- Fix: Change "Ends within" from 1440min to 15min or 1h to scope to active market windows
- Also: Session dedup (`autoModeBoughtSet`) may be blocking repeats from earlier buys — Stop/restart bot clears it
- Next: Test with 15min window during a crypto 15M slot, confirm buys resume

---

## Recently Resolved

- 2026-05-28 — Set up cross-machine sync infrastructure: parent CLAUDE.md pointer,
  SessionStart/Stop hooks, PostToolUse action logging (activity.jsonl), and this WORKLOG.
  Documented full onboarding + every-switch routine in CONTEXT.md ("Multi-Machine Sync
  & Onboarding" section). Any new PC: read CONTEXT.md and follow the ONE-TIME SETUP there.
