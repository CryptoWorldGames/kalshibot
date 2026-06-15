@echo off
REM ============================================================================
REM  KalshiBot launcher + auto-updater  (same logic as run_bot.bat)
REM  Use this if you need to manually restart the bot.
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
