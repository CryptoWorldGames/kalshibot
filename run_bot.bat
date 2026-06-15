@echo off
REM ============================================================================
REM  KalshiBot launcher + auto-updater
REM  Uses `git fetch` + `git checkout` + `git reset --hard` (never `git pull`)
REM  so it can NEVER open a merge-message editor (that was the Notepad hang).
REM
REM  Bot data files (bot_positions.json, settlements_cache.json, etc.) are all
REM  gitignored, so they are UNTRACKED and survive the reset untouched.
REM ============================================================================
set BRANCH=main

:loop
cd /d "%USERPROFILE%\bots\kalshibot"

echo [%date% %time%] Fetching latest from GitHub...
git fetch origin

echo [%date% %time%] Switching to %BRANCH% ...
git checkout %BRANCH% 2>nul || git checkout -b %BRANCH% origin/%BRANCH%

echo [%date% %time%] Hard-syncing to origin/%BRANCH% ...
git reset --hard origin/%BRANCH%

echo [%date% %time%] Starting bot on branch %BRANCH% ...
python kalshibot_app.py

echo [%date% %time%] Bot exited. Restarting in 5 seconds...
timeout /t 5 /nobreak
goto loop
