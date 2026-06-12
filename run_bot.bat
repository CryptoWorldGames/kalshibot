@echo off
:loop
cd C:\Users\mycry\bots\kalshibot
echo [%date% %time%] Pulling latest...
git pull origin main
echo [%date% %time%] Starting bot...
python kalshibot_app.py
echo [%date% %time%] Bot exited. Restarting in 5 seconds...
timeout /t 5 /nobreak
goto loop
