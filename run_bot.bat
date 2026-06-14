@echo off
REM ============================================================================
REM  KalshiBot launcher + auto-updater
REM  Uses `git fetch` + `git checkout` + `git reset --hard` (never `git pull`)
REM  so it can NEVER open a merge-message editor (that was the Notepad hang).
REM
REM  BRANCH is PINNED below to the branch Claude pushes to. Auto-detecting the
REM  current branch was unreliable: if the clone happened to be on `main`, it
REM  pulled old `main` code instead of the live work. Pinning forces the right
REM  branch every launch, regardless of what the clone was last left on.
REM  >>> If Claude ever moves work to a different branch, change BRANCH here. <<<
REM
REM  Bot data files (bot_positions.json, settlements_cache.json, etc.) are all
REM  gitignored, so they are UNTRACKED and survive the reset untouched.
REM ============================================================================
set BRANCH=claude/practical-hawking-0faq18

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
