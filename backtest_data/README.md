# Backtest data (do not delete)

This folder holds the bot's **backtest dataset** — the per-game spread history
the Smart Strategy uses to compute its Scanner/Lotto threshold. It used to live
only in memory and was wiped on every restart. It is now persisted so we can
**never lose backtest data of any type**.

## Where it's saved (two copies, so one loss can't wipe it)

1. **`backtest_data/` (this folder, inside the repo)** — tracked by git, so it
   rides along to **GitHub** as an off-site backup. Commit + push to back it up.
2. **`~/KalshiBots/backtest_data/` (your home directory)** — a copy on the PC
   running the bot, *outside* the repo, so a re-clone or `git clean` can't take
   it. The path is resolved dynamically from `Path.home()`
   (`C:\Users\<you>\...` on Windows, `/home/<you>/...` on Linux/Mac) — it is
   **never hard-coded to a specific machine or username**.

On startup the bot loads whichever copy was saved most recently, so the freshest
data always wins.

## Files

- `game_spread_history.json` — per-token, per-game closing spreads
  (`{symbol: {game_bucket: spread}}`), capped at the last 96 games (24h).

Writes are throttled (~20s) so the closing-window updates don't hammer the disk.
