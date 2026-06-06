@echo off
REM ============================================================
REM  KalshiBot — one-click start
REM  Runs the MANAGER, which (1) launches the bot app.py in its
REM  own window AND (2) enables the green Update button (remote
REM  git-pull + restart from your phone/laptop).
REM  %~dp0 = this file's own folder, so it works no matter where
REM  the bot folder lives. Double-click this, or run it from CMD.
REM ============================================================
cd /d "%~dp0"
echo Starting KalshiBot (manager on 5103 + bot on 5003)...
python kalshi-manager.py
echo.
echo Manager exited. (If you see a python error above, install/fix Python.)
pause
