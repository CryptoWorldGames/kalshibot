# Remote Access Setup for KalshiBot

## Problem
You can't access the bot remotely via Tailscale link: `http://100.x.y.z:5003/`

## Root Cause
Windows Defender Firewall is blocking port 5003 (Flask) from incoming remote connections.

## Solution

### Step 1: Run Firewall Fix (ONE TIME)
1. On your desktop machine (oiokum2)
2. Right-click **FIREWALL_FIX.bat** → "Run as administrator"
3. Wait for ✅ confirmation message
4. Close the window

### Step 2: Restart KalshiBot Manager
1. Close the KalshiBot Manager window (if running)
2. Double-click **start_bot.bat**
3. Wait for Flask to start (you should see "Running on http://0.0.0.0:5003/")

### Step 3: Test Locally (on desktop)
Open browser and try:
```
http://localhost:5003/
```
Should load the KalshiBot UI ✅

### Step 4: Test Remotely (from work via Tailscale)
On your mobile/laptop via Tailscale, try:
```
http://100.x.y.z:5003/
```
Should load the KalshiBot UI ✅

---

## If Still Not Working

### Debug Step 1: Check if Flask is running
Open Command Prompt on desktop:
```bash
netstat -an | findstr :5003
```
You should see:
```
TCP    0.0.0.0:5003     0.0.0.0:0      LISTENING
```

If you DON'T see this, Flask crashed. Restart the manager.

### Debug Step 2: Check Firewall Rules
Open Command Prompt on desktop:
```bash
netsh advfirewall firewall show rule name="KalshiBot*"
```
You should see 4 rules. If not, run FIREWALL_FIX.bat again.

### Debug Step 3: Check Tailscale Connection
On your mobile, open Tailscale app:
- Make sure "Connected" shows at the top (blue toggle ON)
- You should see "your-pc-name" with IP "100.x.y.z"
- Try to ping it: `ping 100.x.y.z` in Command Prompt

### Debug Step 4: Check Router/Network
- If on home WiFi, try local IP instead: `http://192.168.x.x:5003/`
- If on work WiFi (not Tailscale), ask your IT - they may block port 5003

---

## What Works After Firewall Fix
✅ Access bot from work via Tailscale (even on different WiFi)  
✅ Enable/disable Spread Guard from mobile  
✅ Edit bot settings and restart from work  
✅ Monitor trading activity live  
✅ Bot auto-updates code when you push to GitHub  

---

## Why This Works
- **Flask** listens on all network interfaces (0.0.0.0:5003)
- **Firewall rules** allow port 5003 through Windows Defender
- **Tailscale** creates a secure tunnel so you can access your desktop from anywhere
- **Manager** auto-restarts Flask if it crashes (no intervention needed)

---

## Remote Workflow at Work
1. Open `http://100.x.y.z:5003/` on mobile
2. Check **Summary** tab - bot is alive and buying/selling
3. Check **Positions** tab - see current trades
4. Toggle **Spread Guard** ON/OFF in Scanner tab if needed
5. If you need to edit code: tell me on Slack, I push fix, bot auto-updates in 5 min

**Nothing to do on your end - bot handles everything.**
