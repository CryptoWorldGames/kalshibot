# KalshiBot Setup Guide

## Prerequisites
- Python 3.8+
- A Kalshi account with API access
- Your own Kalshi API key

## Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/CryptoWorldGames/kalshibot.git
   cd kalshibot
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Get your Kalshi API key:**
   - Log in to [Kalshi](https://kalshi.com)
   - Go to Settings → API
   - Generate a new API key (keep it private!)

4. **Save your credentials locally (not in GitHub):**
   - Copy `kalshi_api_key.example` to `kalshi_api_key`
   - Paste your API key UUID into the file:
     ```
     your_actual_api_key_uuid_here
     ```
   - **⚠️ Never commit this file to GitHub** (already in .gitignore)
   - **⚠️ Never share this key with anyone**

5. **Get your Kalshi private key:**
   - Download your RSA private key from Kalshi Settings
   - Save it as `kalshi_private_key` (also git-ignored)
   - Keep this file private!

6. **Run the bot:**
   ```bash
   python kalshibot_app.py
   ```
   - Flask server starts on `http://localhost:5003`
   - Open in browser to access the web UI

## Important Security Notes

⚠️ **Your API key is your account.** Treat it like a password:
- Never share it
- Never commit it to GitHub
- Never post it in logs or screenshots
- Rotate it if you suspect it's been exposed

✅ **Good practice:**
- Keep `kalshi_api_key` in your local folder only
- Use `.gitignore` to prevent accidental commits
- If you fork this repo, verify `.gitignore` has `kalshi_api_key`

## Usage

Once running:
1. Open `http://localhost:5003` in your browser
2. Configure your trading strategy in the UI
3. Click "Start Bot" to begin trading
4. Monitor activity in the Summary tab

## Troubleshooting

**"API key not found" error:**
- Check that `kalshi_api_key` file exists in the same directory as `kalshibot_app.py`
- Verify the file contains your Kalshi API key UUID
- No extra whitespace or quotes

**"Connection refused" error:**
- Flask is not running - make sure `python kalshibot_app.py` is still executing
- Try a different port if 5003 is already in use

**Can't log in to Kalshi:**
- Verify your API key is correct
- Check your Kalshi account hasn't been locked
- Try generating a new API key in Kalshi Settings

## Remote Access (Optional)

To access the bot from another device:
- Use [Tailscale](https://tailscale.com/) for secure remote access
- Or expose via your own VPN
- Never expose to the public internet directly

## Questions?

See the GitHub repo issues or documentation for more help.
