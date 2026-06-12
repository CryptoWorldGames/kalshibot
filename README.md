# KalshiBot 🤖

A Flask-based prediction market trading bot for Kalshi with real-time scanning, automated buying/selling, and intelligent strategy optimization.

## Quick Start

1. **Start the bot:**
   ```bash
   cd "kalshi bot"
   python app.py
   ```
   Open http://localhost:5000

2. **Configure filters** (Scanner tab):
   - Set buy probability range (e.g., 80-96%)
   - Choose time window (24h recommended)
   - Enable categories (Crypto, Sports, etc.)

3. **Set strategy** (right panel):
   - Profit targets (sell at % or $)
   - Stop-loss protection (auto-sell if losing X%)
   - Buy amount per contract ($1-5 recommended)

4. **Hit "Start Bot"** - it will scan every 30 seconds and buy automatically

## Features

### 🎯 Smart Scanning
- Scans Kalshi markets every 30 seconds
- Filters by probability, time window, categories
- Finds markets matching your criteria
- Displays odds, volume, time to expiration

### 💰 Automated Trading
- **Buy**: Automatically purchases at target probability
- **Sell**: Exits when profit target or stop-loss triggered
- **Limit→Market Fallback**: Tries limit order first, falls back to market if profitable
- **Fractional Shares**: Buy 0.08 contracts, automatically converts to 1¢ increments

### 📊 Position Management
- View all open positions with live P&L
- See settlement ticker with recent wins/losses
- Group positions by expiration time
- Filter by ticker, category, side (YES/NO)
- Sticky headers for easy scrolling

### 🎪 Bidirectional Strategy
- **Buy UP (YES)** - Buy when price low, sell when high
- **Buy DOWN (NO)** - Buy when price high, sell when low
- Both can run simultaneously with independent settings

### 🛡️ Stop-Loss Protection
- **Dollar Amount**: "Auto-sell if I lose $5"
- **Percentage**: "Auto-sell if I lose 15%"
- Works alongside profit targets
- Real-time conflict detection with visual warnings

### 🧠 Coach (AI Suggestions)
Analyzes your settings and trade history to suggest:
- "Your 80-96% range is conservative - try 50-96% for more buys"
- "Your 15-min window is restrictive - expand to 60 min"
- Which categories are historically profitable
- Optimal price bands and trading hours
- Pre-built ranked strategies

### 📈 Performance Tracking
- Win rate by category, price band, side, hour
- Total P&L with detailed breakdown
- Historical settlement analysis
- Best/worst price ranges
- Profitable vs losing categories

### ⚙️ Advanced Controls

**Monitor Thread** (every 45 seconds):
- Checks if positions hit profit targets
- Checks if positions hit stop-loss
- Auto-sells and marks trigger type (profit vs stop-loss)

**Limit Order Fallback**:
- Tries limit order first (better fills)
- Falls back to market order only if profitable
- Prevents buying at a loss

**Progressive Loading**:
- Phase 1: Load positions & balance (fast, <2s)
- Phase 2: Enrich with market data (background, 5-10s)
- Phase 3: Track settlements (continuous)

## Configuration Guide

### Filters (Left Panel)

| Setting | Example | Effect |
|---------|---------|--------|
| **Buy Range** | 80-96% | Only buy when market priced at 80-96% |
| **Time Window** | 24h | Only buy markets expiring within 24 hours |
| **Categories** | Crypto, Sports | Enable which prediction types to buy |
| **Spread** | ±5 | Adjust range by ±5% (e.g., 75-101%) |

### Strategy (Right Panel)

| Setting | Example | Effect |
|---------|---------|--------|
| **Sell at %** | 10% | Auto-sell when profit reaches 10% |
| **Sell at $** | $0.25 | Auto-sell when profit reaches $0.25 |
| **Stop Loss %** | 20% | Auto-sell if you lose 20% on that position |
| **Stop Loss $** | $5 | Auto-sell if you lose $5 on that position |
| **Buy Amount** | $1 | Spend $1 per buy |
| **Max Buys/Scan** | 5 | Buy max 5 contracts per 30-second scan |

### Auto Mode Controls

| Control | Options | Purpose |
|---------|---------|---------|
| **Run Until** | Stopped / After X buys / Until stopped | When to stop auto-buying |
| **Don't Auto-Sell If** | Resolves in <2 min | Skip selling if market ends too soon |
| **Hide Fractional** | On/Off | Hide positions <1 contract |

## Tabs Explained

### Scanner (Main)
Real-time market scanning dashboard. Set filters and hit "Start Bot" or "Scan Now".

### Positions
View all open positions with live P&L. See what you own and how much you're up/down.

### Settlements
Recent completed trades. See your wins/losses and P&L history.

### Coach
AI-powered strategy recommendations based on your settings and trading history.

### Instructions
Detailed feature guide (this content but interactive).

### Debug
Server diagnostics and live scan preview (for troubleshooting).

## Troubleshooting

### Bot Not Buying
- Check if "Start Bot" button is active (blue)
- Check Coach tab for filter tips
- Verify buy range (80-96%) includes any markets
- Check "Max spend" and cash available

### Positions Not Refreshing
- Hard refresh (Ctrl+Shift+R) to clear cache
- Check if you're on Positions tab
- Check if positions refreshed in last 8 seconds

### Sell Not Triggering
- Check if position reached profit target or stop-loss
- Monitor runs every 45 seconds, allow time to trigger
- Check if "Don't auto-sell if <X min" is blocking it

### Limit Orders Failing
- Bot automatically falls back to market order
- Only if the market order would be profitable
- Check if you have enough cash

## API Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/portfolio` | GET | Get positions with optional enrichment |
| `/api/sell` | POST | Sell position (with count & strategy) |
| `/api/strategy` | POST | Update profit/stop-loss settings |
| `/api/coach` | GET | Get AI recommendations |
| `/api/stats` | GET | Get historical performance |
| `/api/pnl` | GET | Get P&L by time period |
| `/api/enrich-positions` | GET | Fetch market data for tickers |

## Performance Notes

- **Scan Interval**: 30 seconds (slower = fewer API calls, faster = more buys)
- **Monitor Interval**: 45 seconds (checks for auto-sells)
- **Market Cache**: 60 seconds (reuses market data)
- **Failed Lookup Cache**: 5 minutes (avoids repeated fails)
- **API Rate Limit**: ~50 calls/second (plenty of headroom)

## Files

```
kalshi bot/
├── app.py              # Flask backend, scanning logic, strategy
├── index.html          # Frontend UI, all tabs, controls
├── bot_positions.json  # Persisted position tracking
├── kalshi_scan_log.txt # Scan history
├── README.md           # This file
└── CONTEXT.md          # Project context (for Claude)
```

## Tips for Best Results

1. **Start Conservative**: Begin with 85-96% range, expand to 50-96% once comfortable
2. **Time Windows**: 24h windows catch more opportunities than 15-min
3. **Position Sizing**: $1-2 per buy spreads risk better than $5+
4. **Stop Loss**: Set to 20-30% protection against bad entries
5. **Monitor Regularly**: Check Coach tab for optimization tips
6. **Multiple Categories**: Trading Crypto + Sports + Politics gives more variety
7. **Profit Targets**: 5-10% targets more realistic than 1-2%

## Known Limitations

- Only trades Kalshi markets (not other platforms)
- Limit orders on very high probability (95%+) may not fill
- Scan finds markets 30+ seconds after they open
- Resolution contracts not tradeable
- Max position size limited by Kalshi API

## License

Private use only. Do not redistribute.

---

**Questions?** Check the Instructions tab in-app for detailed walkthroughs of each feature.
