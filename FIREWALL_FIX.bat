@echo off
REM ============================================================
REM KalshiBot Firewall Fix - Run as Administrator
REM Allows port 5003 (Flask) through Windows Defender Firewall
REM so you can access the bot remotely via Tailscale
REM ============================================================

setlocal enabledelayedexpansion

REM Check if running as admin
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo ERROR: This script requires Administrator privileges.
    echo Please right-click and select "Run as administrator"
    echo.
    pause
    exit /b 1
)

echo.
echo Adding Windows Firewall rules for KalshiBot...
echo.

REM Get Python executable path (adjust if different)
for /f "delims=" %%i in ('where python') do set PYTHON_EXE=%%i

if "%PYTHON_EXE%"=="" (
    echo ERROR: Python not found in PATH
    echo Install Python or add it to your PATH
    pause
    exit /b 1
)

echo Python found at: %PYTHON_EXE%
echo.

REM Rule 1: Allow Python process
echo Adding inbound rule for Python...
netsh advfirewall firewall add rule name="KalshiBot-Python-In" dir=in action=allow program="%PYTHON_EXE%" enable=yes description="Allow KalshiBot Flask server" >nul 2>&1

REM Rule 2: Allow port 5003 inbound
echo Adding port 5003 inbound rule...
netsh advfirewall firewall add rule name="KalshiBot-Port5003-In" dir=in action=allow protocol=tcp localport=5003 enable=yes description="Allow KalshiBot Flask on port 5003" >nul 2>&1

REM Rule 3: Allow port 5003 outbound
echo Adding port 5003 outbound rule...
netsh advfirewall firewall add rule name="KalshiBot-Port5003-Out" dir=out action=allow protocol=tcp localport=5003 enable=yes description="Allow KalshiBot Flask outbound" >nul 2>&1

REM Rule 4: Allow port 5103 (Manager) inbound
echo Adding port 5103 (Manager) inbound rule...
netsh advfirewall firewall add rule name="KalshiBot-Port5103-In" dir=in action=allow protocol=tcp localport=5103 enable=yes description="Allow KalshiBot Manager on port 5103" >nul 2>&1

echo.
echo ✅ Firewall rules added successfully!
echo.
echo You can now:
echo   • Access bot at http://YOUR_TAILSCALE_IP:5003/ from work
echo   • Or access at http://desktop-name:5003/ on home WiFi
echo.
echo To verify rules were added:
echo   netsh advfirewall firewall show rule name="KalshiBot*"
echo.
pause
