@echo off
REM Auto-pull and restart KalshiBot
REM Run once, bot stays updated forever

:loop
cd C:\Users\mycry\bots\kalshibot

REM Kill any running bot
taskkill /F /IM python.exe 2>nul

REM Pull latest
git pull origin main

REM Wait a moment
timeout /t 2 /nobreak

REM Restart bot
python kalshibot_app.py

REM If it crashes, restart in 5 seconds
timeout /t 5 /nobreak
goto loop
