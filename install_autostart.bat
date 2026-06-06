@echo off
REM ============================================================
REM  KalshiBot - install auto-start on login  (run ONCE)
REM  Double-click this once. It makes the bot launch automatically
REM  every time you log into Windows, so a reboot or power outage
REM  brings the bot back on its own. Re-run anytime to refresh.
REM  To UNDO: delete the shortcut it prints at the end.
REM ============================================================
setlocal
set "BOTDIR=%~dp0"
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ws=New-Object -ComObject WScript.Shell; $lnk=Join-Path '%STARTUP%' 'KalshiBot.lnk'; $sc=$ws.CreateShortcut($lnk); $sc.TargetPath=(Join-Path '%BOTDIR%' 'start_bot.bat'); $sc.WorkingDirectory='%BOTDIR%'; $sc.Save(); Write-Host ('Created: ' + $lnk)"
echo.
echo Installed. KalshiBot will now auto-start at every Windows login.
echo To undo, delete the shortcut printed above.
pause
