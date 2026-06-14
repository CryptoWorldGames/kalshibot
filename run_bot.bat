@echo off
REM ============================================================================
REM  KalshiBot launcher + auto-updater
REM  Uses `git fetch` + `git reset --hard` instead of `git pull` so it can NEVER
REM  open a merge-message editor (that was the hang that required clicking Notepad
REM  closed). reset --hard force-matches whatever branch you're on to GitHub.
REM  Bot data files (bot_positions.json, settlements_cache.json, etc.) are all
REM  gitignored, so they are UNTRACKED and survive the reset untouched.
REM ============================================================================
:loop
cd /d C:\Users\mycry\bots\kalshibot

echo [%date% %time%] Fetching latest from GitHub...
git fetch origin

REM Detect the branch this clone is on (works for any branch name, incl. slashes)
for /f "delims=" %%b in ('git rev-parse --abbrev-ref HEAD') do set BRANCH=%%b
echo [%date% %time%] Hard-syncing to origin/%BRANCH% ...
git reset --hard origin/%BRANCH%

echo [%date% %time%] Starting bot on branch %BRANCH% ...
python kalshibot_app.py

echo [%date% %time%] Bot exited. Restarting in 5 seconds...
timeout /t 5 /nobreak
goto loop
