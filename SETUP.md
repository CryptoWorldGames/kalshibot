# KalshiBot Setup Guide (Home PC)

## Prerequisites
- Python 3.8+ installed on your PC
- A Kalshi account with API access
- 5 minutes to set up

## Quick Setup (5 minutes)

### Step 1: Download & Extract
1. Download the bot as a ZIP file from GitHub
2. Extract it to a folder on your PC (e.g., `C:\Users\YourName\Documents\kalshibot`)
3. Remember this folder location — you'll need it later

### Step 2: Install Python Dependencies
1. Open **Command Prompt** or **PowerShell**
2. Navigate to your bot folder:
   ```bash
   cd C:\Users\YourName\Documents\kalshibot
   ```
3. Install required libraries:
   ```bash
   pip install -r requirements.txt
   ```
   (This takes 1-2 minutes)

### Step 3: Get Your Kalshi API Keys
1. Log in to [Kalshi.com](https://kalshi.com)
2. Go to **Settings → API**
3. Generate a new API key and copy the UUID (long alphanumeric string)
4. Also download your **RSA private key** file (save it somewhere safe)

### Step 4: Create the Keys Folder
Your bot needs a special folder for credentials (kept separate from the bot folder for security).

1. Create a new folder next to your bot folder:
   - Windows: `C:\Users\YourName\kalshi-keys`
   - Mac/Linux: `~/kalshi-keys`

2. In that folder, create two text files:

   **File 1: `kalshi_api_key`**
   - Open Notepad
   - Paste your Kalshi API key UUID (from Step 3)
   - Save as `kalshi_api_key` (no `.txt` extension!)
   - Place in the `kalshi-keys` folder

   **File 2: `kalshi_private_key`**
   - Open the private key file you downloaded from Kalshi
   - Copy the entire contents
   - Create a new text file in `kalshi-keys` folder
   - Paste the entire key
   - Save as `kalshi_private_key` (no `.txt` extension!)

**⚠️ IMPORTANT:** Never share these files. They give full access to your Kalshi account.

### Step 5: Run the Bot
1. Open **Command Prompt** or **PowerShell** in your bot folder
2. Type:
   ```bash
   python kalshibot_app.py
   ```
3. Wait for it to start — you'll see something like:
   ```
   Running on http://127.0.0.1:5003
   ```

### Step 6: Open the Bot in Your Browser
1. Open your browser (Chrome, Firefox, Edge, Safari)
2. Go to: **http://localhost:5003**
3. You should see the KalshiBot dashboard!

### Step 7: Configure Your Settings
1. **Scanner tab**: Set your buy probability range (e.g., 80-96%)
2. **Set strategy**: Profit targets, stop-loss, buy amount
3. **Click "Start Bot"** — it will scan every 30 seconds

## Troubleshooting

### "API key not found" error
**Fix:** Make sure the folder structure is correct:
```
C:\Users\YourName\
├── kalshi-keys\          ← API keys go HERE
│   ├── kalshi_api_key
│   └── kalshi_private_key
└── Documents\kalshibot\  ← Bot goes HERE
```

### "Connection refused" error
**Fix:** Make sure the bot is running:
- Check Command Prompt shows `Running on http://127.0.0.1:5003`
- If port 5003 is busy, edit `kalshibot_app.py` line 5: change `port=5003` to another number (e.g., 5004)

### Port 5003 already in use
**Fix:** Find what's using it:
```bash
netstat -ano | findstr :5003   # Windows
lsof -i :5003                   # Mac/Linux
```
Then either kill that process or use a different port in `kalshibot_app.py`.

### Bot won't start or closes immediately
- Make sure all 3 files exist: `kalshibot_app.py`, `index.html`, `requirements.txt`
- Try running from Command Prompt (not double-clicking the `.py` file)
- Check Python is installed: `python --version`

### Settings not saving
- Hard refresh browser: **Ctrl+Shift+R** (Windows) or **Cmd+Shift+R** (Mac)
- Clear browser cache
- Close and reopen the bot

## Keeping the Bot Running 24/7 (Optional)

If you want the bot to run continuously while your PC is on:

**Windows:**
1. Create a file called `start_bot.bat` in your bot folder:
   ```batch
   @echo off
   cd C:\Users\YourName\Documents\kalshibot
   python kalshibot_app.py
   pause
   ```
2. Double-click `start_bot.bat` to start
3. Keep the window open

**Mac/Linux:**
1. Create a file called `start_bot.sh` in your bot folder:
   ```bash
   #!/bin/bash
   cd ~/Documents/kalshibot
   python kalshibot_app.py
   ```
2. In terminal: `chmod +x start_bot.sh`
3. Run: `./start_bot.sh`

## Questions?

See the README.md for feature details, or check GitHub Issues for more help.
