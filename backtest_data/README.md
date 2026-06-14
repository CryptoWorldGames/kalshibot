# Backtest data (your own — stays on your computer)

This folder holds **your** backtest dataset — the per-game spread history the
Smart Strategy uses to compute its Scanner/Lotto threshold. It is built
**automatically from Kalshi market data** as your bot runs (it does NOT use your
personal trades). Each person's bot grows its own history.

## Important for everyone who downloads this bot

The actual data file (`game_spread_history.json`) is **git-ignored** — it is
never committed or shared. That keeps downloads clean, avoids merge conflicts
when you pull updates, and means you never inherit anyone else's data. You start
fresh and your bot learns from the live market on your machine.

## Where it's saved (two LOCAL copies, so one loss can't wipe it)

1. **`backtest_data/` (this folder)** — next to the app, ignored by git.
2. **`~/KalshiBots/backtest_data/`** — in your home directory, outside the repo,
   so re-downloading or a `git clean` can't take it. The path is resolved from
   `Path.home()` (`C:\Users\<you>\...` on Windows, `/home/<you>/...` on
   Linux/Mac) — never hard-coded to a specific machine or username.

On startup the bot loads whichever copy was saved most recently.

## Cold start

A brand-new install has no history yet, so for roughly the first ~4 hours the bot
uses a safe fallback threshold (a cushion above the current spread) and switches
to the data-driven 75th-percentile threshold once it has logged enough games
(16 games = 4h per token). Writes are throttled (~20s) so they don't hammer the
disk.

## Files

- `game_spread_history.json` — per-token, per-game closing spreads
  (`{symbol: {game_bucket: spread}}`), capped at the last 96 games (24h).
