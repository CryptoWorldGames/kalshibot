# KalshiBot Manager Setup

## Overview
KalshiBot now uses a dedicated manager process (`KalshiBot_manager.py`) that controls the Flask web server with remote control endpoints.

## Port Configuration
Ports are defined in `.env` file:
- `KALSHI_BOT_PORT`: Flask web server port (default: 5003)
- `KALSHI_MANAGER_PORT`: Manager control port (default: 5103)

Example `.env`:
```
KALSHI_BOT_PORT=5003
KALSHI_MANAGER_PORT=5103
```

## Running the Manager

### Start Manager
```bash
python KalshiBot_manager.py
```

This will:
1. Start Flask on `KALSHI_BOT_PORT`
2. Start manager HTTP server on `KALSHI_MANAGER_PORT`
3. Auto-restart Flask if it crashes
4. Auto-pull and deploy new code from GitHub every 5 minutes

### Manager Endpoints
- `GET /api/manager/status` - Check if Flask is running
- `POST /api/manager/start` - Start Flask
- `POST /api/manager/stop` - Stop Flask
- `POST /api/manager/restart` - Restart Flask
- `POST /api/manager/update` - Git pull + restart Flask

## Multiple Bots Setup

Each bot gets its own:
1. **Manager file**: Named after the bot (e.g., `KalshiBot_manager.py`, `BinanceBot_manager.py`)
2. **.env file**: With unique ports (Kalshi=5003/5103, Binance=5004/5104, etc.)
3. **Directory**: Completely isolated from other bots

### Example for Multiple Bots
```
/home/user/
  ├── kalshibot/
  │   ├── KalshiBot_manager.py
  │   ├── .env (KALSHI_BOT_PORT=5003, KALSHI_MANAGER_PORT=5103)
  │   └── app.py
  ├── binancebot/
  │   ├── BinanceBot_manager.py
  │   ├── .env (BINANCE_BOT_PORT=5004, BINANCE_MANAGER_PORT=5104)
  │   └── app.py
  └── cnsbot/
      ├── CNSBot_manager.py
      ├── .env (CNS_BOT_PORT=5001, CNS_MANAGER_PORT=5101)
      └── app.py
```

## Isolation
- Each manager reads from its own `.env` file in its directory
- Ports are explicitly pinned (no auto-assignment)
- Restarting one bot's manager only affects that bot
- No cross-bot dependencies or interference

## Notes
- Manager runs with `CREATE_NEW_CONSOLE` on Windows for visibility in Task Manager
- Flask process is launched with full path for clarity
- .env is parsed manually (no external dependencies)
