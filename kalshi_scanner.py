#!/usr/bin/env python3
"""
Kalshi Scanner Bot
Scans all open markets for ones closing within N minutes that have a win
probability >= threshold%, then auto-buys a fixed dollar amount.

Usage examples:
  python kalshi_scanner.py --paper              # paper mode (no real money)
  python kalshi_scanner.py                      # live mode (prompts for confirmation)
  python kalshi_scanner.py --threshold 90 --minutes 10 --amount 5
  python kalshi_scanner.py --once --paper       # scan once and exit
  python kalshi_scanner.py --interval 30        # scan every 30 seconds

Flags:
  --paper        Simulate orders, no real money spent
  --threshold N  Minimum probability % to qualify (default: 85)
  --minutes N    Look for markets closing within N minutes (default: 15)
  --amount N     Dollars to spend per qualifying market (default: 3.00)
  --interval N   Seconds between scans in continuous mode (default: 60)
  --once         Run a single scan then exit

Credentials (read from files in the same directory as this script):
  kalshi_api_key  — your Kalshi API key (plain text)
  test2.txt       — your RSA private key in PEM format
"""

import argparse
import base64
import json
import math
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://api.elections.kalshi.com"
API_PREFIX = "/trade-api/v2"
HERE = Path(__file__).parent


# ---------------------------------------------------------------------------
# Auth & HTTP helpers
# ---------------------------------------------------------------------------

def load_credentials():
    """Read API key and RSA private key from local files."""
    api_key = (HERE / "kalshi_api_key").read_text().strip()
    pem = (HERE / "test2.txt").read_bytes()
    private_key = serialization.load_pem_private_key(
        pem, password=None, backend=default_backend()
    )
    return api_key, private_key


def _auth_headers(api_key, private_key, method: str, path: str) -> dict:
    """Build Kalshi HMAC-style auth headers using RSA-SHA256."""
    ts = str(int(time.time() * 1000))
    message = (ts + method.upper() + path).encode()
    sig = base64.b64encode(
        private_key.sign(message, asym_padding.PKCS1v15(), hashes.SHA256())
    ).decode()
    return {
        "KALSHI-ACCESS-KEY": api_key,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "Content-Type": "application/json",
    }


def api_get(api_key, pk, endpoint: str, params: dict = None) -> dict:
    path = API_PREFIX + endpoint
    r = requests.get(
        BASE_URL + path,
        headers=_auth_headers(api_key, pk, "GET", path),
        params=params or {},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def api_post(api_key, pk, endpoint: str, body: dict) -> dict:
    path = API_PREFIX + endpoint
    body_str = json.dumps(body)
    r = requests.post(
        BASE_URL + path,
        headers=_auth_headers(api_key, pk, "POST", path),
        data=body_str,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Market fetching & filtering
# ---------------------------------------------------------------------------

def fetch_all_open_markets(api_key, pk) -> list:
    """Paginate through every open market on Kalshi."""
    markets = []
    cursor = None
    while True:
        params = {"status": "open", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = api_get(api_key, pk, "/markets", params)
        batch = data.get("markets", [])
        markets.extend(batch)
        cursor = data.get("cursor")
        if not cursor or not batch:
            break
    return markets


def _parse_close_time(market: dict):
    """Return a UTC-aware datetime for the market's close time, or None."""
    for field in ("close_time", "expiration_time"):
        val = market.get(field)
        if val:
            try:
                return datetime.fromisoformat(val.replace("Z", "+00:00"))
            except ValueError:
                pass
    return None


def filter_qualifying(markets: list, threshold_pct: float, minutes: int) -> list:
    """
    Return (market, side, price_cents) for markets that:
      - close within the next `minutes` minutes
      - have yes_ask >= threshold_pct  OR  no_ask >= threshold_pct
    If both sides qualify (unusual), prefers YES.
    """
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(minutes=minutes)
    results = []

    for m in markets:
        ct = _parse_close_time(m)
        if ct is None or not (now < ct <= cutoff):
            continue

        yes_ask = m.get("yes_ask")  # cents (0–100), cost to buy 1 YES contract
        no_ask = m.get("no_ask")   # cents (0–100), cost to buy 1 NO contract

        if yes_ask is not None and yes_ask >= threshold_pct:
            results.append((m, "yes", int(yes_ask)))
        elif no_ask is not None and no_ask >= threshold_pct:
            results.append((m, "no", int(no_ask)))

    return results


# ---------------------------------------------------------------------------
# Orderbook & slippage
# ---------------------------------------------------------------------------

def _parse_ob_levels(raw) -> list:
    """Normalize orderbook levels to list of (price_cents, quantity)."""
    out = []
    for lvl in (raw or []):
        if isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
            out.append((int(lvl[0]), int(lvl[1])))
        elif isinstance(lvl, dict):
            out.append((int(lvl.get("price", 0)), int(lvl.get("quantity", 0))))
    return sorted(out, key=lambda x: x[0])  # ascending price


def estimate_fill(orderbook: dict, side: str, dollars: float):
    """
    Walk the ask side of the orderbook to estimate fill price and slippage.
    Returns (avg_fill_cents, num_contracts, slippage_pct) or None if no liquidity.
    """
    levels = _parse_ob_levels(orderbook.get(side))
    if not levels:
        return None

    best_price = levels[0][0]
    remaining = dollars
    total_cost = 0.0
    total_qty = 0

    for price_c, qty in levels:
        if remaining <= 0:
            break
        price_d = price_c / 100
        can_buy = min(qty, math.floor(remaining / price_d))
        if can_buy < 1:
            break
        total_cost += can_buy * price_d
        total_qty += can_buy
        remaining -= can_buy * price_d

    if total_qty == 0:
        return None

    avg_c = (total_cost / total_qty) * 100
    slippage = ((avg_c - best_price) / best_price * 100) if best_price else 0.0
    return avg_c, total_qty, slippage


# ---------------------------------------------------------------------------
# Order placement
# ---------------------------------------------------------------------------

def place_order(api_key, pk, ticker: str, side: str, count: int, paper: bool) -> dict:
    if paper:
        return {"paper": True, "ticker": ticker, "side": side, "count": count}
    body = {
        "ticker": ticker,
        "action": "buy",
        "side": side,
        "type": "market",
        "count": count,
    }
    return api_post(api_key, pk, "/portfolio/orders", body)


# ---------------------------------------------------------------------------
# Core scan loop
# ---------------------------------------------------------------------------

def scan_once(api_key, pk, cfg, bought_tickers: set):
    threshold = cfg.threshold
    minutes = cfg.minutes
    dollars = cfg.amount
    paper = cfg.paper
    now_str = datetime.now().strftime("%H:%M:%S")

    print(f"\n[{now_str}] Scanning all open markets...")

    try:
        all_markets = fetch_all_open_markets(api_key, pk)
    except requests.HTTPError as e:
        print(f"  HTTP error fetching markets: {e.response.status_code} {e.response.text[:200]}")
        return
    except Exception as e:
        print(f"  Error fetching markets: {e}")
        return

    qualifying = filter_qualifying(all_markets, threshold, minutes)
    total = len(all_markets)

    if not qualifying:
        print(f"  Scanned {total} markets — none qualify (≥{threshold}% closing in ≤{minutes}min)")
        return

    print(f"  Scanned {total} markets — {len(qualifying)} qualify:")

    for market, side, price_c in qualifying:
        ticker = market.get("ticker", "?")
        title = market.get("title", ticker)
        ct = market.get("close_time") or market.get("expiration_time", "?")

        # How many contracts can we buy?
        price_d = price_c / 100
        contracts = math.floor(dollars / price_d)

        print(f"\n  {'─'*58}")
        print(f"  Market  : {title}")
        print(f"  Ticker  : {ticker}")
        print(f"  Side    : {side.upper()}")
        print(f"  Price   : {price_c}¢  ({price_c}% implied probability)")
        print(f"  Closes  : {ct}")

        if contracts < 1:
            print(f"  Skip    : ${dollars:.2f} is too small for 1 contract at {price_c}¢")
            continue

        # Slippage estimate from orderbook
        try:
            ob_resp = api_get(api_key, pk, f"/markets/{ticker}/orderbook")
            ob = ob_resp.get("orderbook", {})
            fill = estimate_fill(ob, side, dollars)
            if fill:
                avg_c, contracts, slippage = fill
                print(f"  Est fill: {avg_c:.1f}¢  |  Contracts: {contracts}  |  Slippage: {slippage:.2f}%")
                print(f"  Est cost: ${contracts * avg_c / 100:.2f}")
            else:
                print(f"  Orderbook: no asks visible — using best ask for sizing")
                print(f"  Contracts: {contracts}  |  Est cost: ${contracts * price_d:.2f}")
        except Exception as e:
            print(f"  Orderbook unavailable ({e}); using {contracts} contracts at {price_c}¢")

        if ticker in bought_tickers:
            print(f"  Status  : Already bought this session — skipping duplicate")
            continue

        mode_tag = "[PAPER]" if paper else "[LIVE] "
        print(f"  {mode_tag} Placing market buy: {contracts} {side.upper()} on {ticker}")

        try:
            result = place_order(api_key, pk, ticker, side, contracts, paper)
            bought_tickers.add(ticker)
            if paper:
                print(f"  Result  : Paper order logged (no real order sent)")
            else:
                order = result.get("order", result)
                oid = order.get("order_id", "?")
                status = order.get("status", "?")
                print(f"  Result  : Order ID {oid} | Status: {status}")
        except requests.HTTPError as e:
            print(f"  Error   : {e.response.status_code} — {e.response.text[:200]}")
        except Exception as e:
            print(f"  Error   : {e}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Kalshi Scanner Bot — scans for high-probability short-expiry markets and buys.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--paper", action="store_true",
        help="Paper mode: log orders without spending real money",
    )
    parser.add_argument(
        "--threshold", type=float, default=85.0, metavar="PCT",
        help="Minimum win probability %% to qualify (default: 85)",
    )
    parser.add_argument(
        "--minutes", type=int, default=15, metavar="N",
        help="Buy markets closing within N minutes (default: 15)",
    )
    parser.add_argument(
        "--amount", type=float, default=3.0, metavar="DOLLARS",
        help="Dollar amount to spend per qualifying market (default: 3.00)",
    )
    parser.add_argument(
        "--interval", type=int, default=60, metavar="SEC",
        help="Seconds between scans in continuous mode (default: 60)",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run one scan then exit (default: loop forever)",
    )
    cfg = parser.parse_args()

    print("=" * 60)
    print("  Kalshi Scanner Bot")
    print("=" * 60)
    print(f"  Mode      : {'PAPER — no real orders' if cfg.paper else '*** LIVE — REAL MONEY ***'}")
    print(f"  Threshold : {cfg.threshold}%+")
    print(f"  Lookahead : {cfg.minutes} minutes")
    print(f"  Buy size  : ${cfg.amount:.2f} per market")
    if not cfg.once:
        print(f"  Interval  : every {cfg.interval}s (Ctrl+C to stop)")
    print("=" * 60)

    if not cfg.paper:
        confirm = input("\n  WARNING: LIVE mode uses real money.\n  Type YES to confirm: ").strip()
        if confirm != "YES":
            print("  Aborted.")
            sys.exit(0)

    try:
        api_key, pk = load_credentials()
        print("\n  Credentials loaded OK.\n")
    except FileNotFoundError as e:
        print(f"\n  Credential file not found: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n  Failed to load credentials: {e}")
        sys.exit(1)

    bought_tickers: set = set()

    if cfg.once:
        scan_once(api_key, pk, cfg, bought_tickers)
    else:
        try:
            while True:
                scan_once(api_key, pk, cfg, bought_tickers)
                time.sleep(cfg.interval)
        except KeyboardInterrupt:
            print("\n\n  Stopped.")
            if bought_tickers:
                print(f"  Tickers bought this session: {', '.join(sorted(bought_tickers))}")

    sys.exit(0)


if __name__ == "__main__":
    main()
