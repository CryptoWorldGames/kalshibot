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

_(nothing in progress)_

---

## Recently Resolved

- 2026-05-28 — Set up cross-machine sync infrastructure: parent CLAUDE.md pointer,
  SessionStart/Stop hooks, PostToolUse action logging (activity.jsonl), and this WORKLOG.
  Documented full onboarding + every-switch routine in CONTEXT.md ("Multi-Machine Sync
  & Onboarding" section). Any new PC: read CONTEXT.md and follow the ONE-TIME SETUP there.
