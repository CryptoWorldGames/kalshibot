#!/usr/bin/env python3
"""
Kalshi Scanner — Flask backend
Run: python app.py   →   open http://localhost:5000
"""

import base64
import json
import math
import os
import sys
import threading
import time
import uuid

# Force UTF-8 stdout/stderr — Windows cp1252 default chokes on arrows, emoji, etc.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests as req
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding, ec
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey
from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__)
HERE = Path(__file__).resolve().parent
BASE_URL = "https://api.elections.kalshi.com"
API_PREFIX = "/trade-api/v2"
DEBUG_LOGGING = False  # Set to True for verbose logs, False for production

# Category detection cache — avoid re-detecting same market
_category_cache = {}
_CACHE_MAX_SIZE = 5000

CRYPTO_KEYWORDS = {
    "btc", "eth", "sol", "doge", "xrp", "ada", "avax", "matic", "link",
    "bitcoin", "ethereum", "solana", "crypto", "coinbase", "binance",
    "pepe", "shib", "bnb", "ton", "sui", "apt",
    "hype", "fet", "atom", "near", "uni", "ltc", "trx", "fil", "icp",
    "ftm", "algo", "vet", "hbar", "egld", "xlm", "etc", "bch", "kas",
    "tao", "render", "rndr", "pyth", "jto", "wif", "bonk", "tia",
    "ondo", "ena", "jup", "sei", "pendle", "arb", "op", "blast",
}

# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

CREDS_DIR = Path(os.environ.get("KALSHI_CREDS_DIR", HERE.parent / "kalshi-keys"))

def _load_creds():
    key = (CREDS_DIR / "kalshi_api_key").read_text(encoding="utf-8").lstrip("﻿").strip()
    pem = (CREDS_DIR / "test2.txt").read_bytes()
    pk = serialization.load_pem_private_key(pem, password=None, backend=default_backend())
    return key, pk

try:
    API_KEY, PRIVATE_KEY = _load_creds()
    key_type = "RSA" if isinstance(PRIVATE_KEY, RSAPrivateKey) else "EC" if isinstance(PRIVATE_KEY, EllipticCurvePrivateKey) else type(PRIVATE_KEY).__name__
    print(f"Credentials loaded OK. Key prefix: {API_KEY[:8]}... len={len(API_KEY)} | Private key type: {key_type}")
    print(f"Creds dir: {CREDS_DIR}")
except Exception as e:
    print(f"ERROR loading credentials: {e}")
    raise

# ---------------------------------------------------------------------------
# Auth & HTTP
# ---------------------------------------------------------------------------

def _sign(message: bytes) -> bytes:
    if isinstance(PRIVATE_KEY, RSAPrivateKey):
        return PRIVATE_KEY.sign(message, asym_padding.PSS(
            mgf=asym_padding.MGF1(hashes.SHA256()),
            salt_length=asym_padding.PSS.MAX_LENGTH), hashes.SHA256())
    elif isinstance(PRIVATE_KEY, EllipticCurvePrivateKey):
        return PRIVATE_KEY.sign(message, ec.ECDSA(hashes.SHA256()))
    raise ValueError(f"Unsupported key type: {type(PRIVATE_KEY)}")

def _headers(method: str, path: str, body: str = "") -> dict:
    ts = str(int(time.time() * 1000))
    sig = base64.b64encode(_sign((ts + method.upper() + path + body).encode())).decode()
    return {
        "KALSHI-ACCESS-KEY": API_KEY,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "Content-Type": "application/json",
    }

def kalshi_get(endpoint: str, params: dict = None) -> dict:
    path = API_PREFIX + endpoint
    r = req.get(BASE_URL + path, headers=_headers("GET", path),
                 params=params or {}, timeout=20)  # 20s timeout
    if not r.ok:
        print(f"[API {r.status_code}] GET {endpoint} -> {r.text[:500]}")
    r.raise_for_status()
    return r.json()

def kalshi_post(endpoint: str, body: dict) -> dict:
    path = API_PREFIX + endpoint
    body_str = json.dumps(body, separators=(',', ':'))
    r = req.post(BASE_URL + path, headers=_headers("POST", path),
                  data=body_str, timeout=15)
    if not r.ok:
        print(f"[API {r.status_code}] POST {endpoint} -> {r.text[:500]}")
    r.raise_for_status()
    return r.json()

# ---------------------------------------------------------------------------
# Position tracking & sell strategy
# ---------------------------------------------------------------------------

_lock = threading.Lock()

TRACKED_FILE   = HERE / "bot_positions.json"
STRATEGY_FILE  = HERE / "bot_strategy.json"
SCAN_LOG       = HERE / "scan_log.jsonl"      # append-only; one JSON line per scan run

def _save_tracked():
    try:
        TRACKED_FILE.write_text(json.dumps(tracked, default=str), encoding="utf-8")
    except Exception as e:
        print(f"[tracked] save error: {e}")

# { ticker: { side, count, buy_price, title, strategy, target_pct, bought_at, status } }
tracked: dict = {}
try:
    if TRACKED_FILE.exists():
        tracked = json.loads(TRACKED_FILE.read_text(encoding="utf-8"))
        print(f"Loaded {len(tracked)} tracked positions from {TRACKED_FILE.name}")
except Exception as e:
    print(f"[tracked] load error: {e}")

# Sell strategy settings (updated from frontend)
sell_settings = {
    "skip_auto_sell_near_resolution": True,
    "skip_auto_sell_minutes": 1,
}

# Global sell strategy — load from file so it survives Flask restarts
try:
    sell_strategy = json.loads(STRATEGY_FILE.read_text(encoding="utf-8")) if STRATEGY_FILE.exists() else {}
except Exception:
    sell_strategy = {}
sell_strategy.setdefault("mode", "resolution")
sell_strategy.setdefault("target_pct", 10.0)
print(f"[strategy] loaded: mode={sell_strategy.get('mode')} target_pct={sell_strategy.get('target_pct')} target_dollars={sell_strategy.get('target_dollars')}")

# In-memory cache of event_ticker → clean title
_event_cache: dict = {}

# Settlements cache — shared between stats, coach, portfolio (avoids 3x duplicate queries)
_settlements_cache: dict = {}  # hours → {"data": [...], "ts": float}
_SETTLEMENTS_CACHE_TTL = 120.0  # 2 minutes

def _cached_settlements(hours: int = 24) -> list:
    """Return settlements from cache if fresh, otherwise fetch and cache."""
    now = time.time()
    cached = _settlements_cache.get(hours)
    if cached and (now - cached["ts"]) < _SETTLEMENTS_CACHE_TTL:
        return cached["data"]
    data = _recent_settlements(hours=hours)
    _settlements_cache[hours] = {"data": data, "ts": now}
    return data

# Market data cache — avoid hitting /markets/{ticker} more than once per 60s
_market_cache: dict = {}   # ticker → {"data": {...}, "ts": float}
_MARKET_CACHE_TTL = 60.0   # seconds
_failed_market_cache: set = set()  # tickers that 404/failed — don't retry for 5 min
_failed_market_ts: dict = {}  # ticker → timestamp of failure

def _get_market(ticker: str) -> dict:
    """Fetch market data with 60s cache to reduce Kalshi API calls."""
    now = time.time()
    cached = _market_cache.get(ticker)
    if cached and (now - cached["ts"]) < _MARKET_CACHE_TTL:
        return cached["data"]
    # Skip known-failed tickers for 5 minutes
    fail_ts = _failed_market_ts.get(ticker)
    if fail_ts and (now - fail_ts) < 300:
        return {}
    try:
        data = kalshi_get(f"/markets/{ticker}").get("market", {})
    except Exception:
        data = {}
    if data:
        _market_cache[ticker] = {"data": data, "ts": now}
    else:
        # Cache the failure so we don't spam the API
        _failed_market_ts[ticker] = now
    return data

def _get_sold_by(ticker: str) -> str:
    """Return how a position was closed: Bot Auto-Sell, Bot (manual), or Human."""
    pos = tracked.get(ticker)
    if not pos:
        return "Human"
    sold_by = pos.get("sold_by", "")
    if sold_by == "bot_auto":
        return "Bot Auto-Sell"
    if sold_by == "human":
        return "Human"
    if ticker in tracked:
        return "KalshiBot"
    return "Human"

def _kalshi_url(event_ticker: str, ticker: str) -> str:
    """Build Kalshi market URL. Derives event_ticker from ticker if missing."""
    evt = event_ticker or ""
    if not evt and ticker:
        # Derive event_ticker: everything up to but not including the last '-segment'
        parts = ticker.rsplit("-", 1)
        evt = parts[0] if len(parts) == 2 else ticker
    if evt and ticker:
        return f"https://kalshi.com/markets/{evt}/{ticker}"
    return ""

def _event_title(event_ticker: str) -> str:
    """Fetch the human-readable event title from Kalshi's events endpoint (cached)."""
    if not event_ticker:
        return ""
    if event_ticker in _event_cache:
        return _event_cache[event_ticker]
    try:
        data = kalshi_get(f"/events/{event_ticker}")
        evt  = data.get("event", {})
        title = evt.get("title") or evt.get("event_title") or ""
        _event_cache[event_ticker] = title
        return title
    except Exception:
        _event_cache[event_ticker] = ""
        return ""


def is_crypto(market: dict) -> bool:
    text = " ".join([
        market.get("ticker", ""),
        market.get("title", ""),
        market.get("category", ""),
        market.get("event_ticker", ""),
    ]).lower()
    return any(kw in text for kw in CRYPTO_KEYWORDS)


COMBO_KEYWORDS = {"combo", "parlay", "multi"}

def is_sports(market: dict) -> bool:
    """Check if market is sports-related (NFL, NBA, MLB, NHL, etc.)"""
    ticker   = (market.get("ticker", "") or "").lower()
    title    = (market.get("title", "") or "").lower()
    category = (market.get("category", "") or "").lower()

    sports_keywords = {"nfl", "nba", "mlb", "nhl", "soccer", "tennis", "golf", "mma", "ufc", "boxing", "horse"}
    for kw in sports_keywords:
        if kw in ticker or kw in title or kw in category:
            return True
    return False


def is_politics(market: dict) -> bool:
    """Check if market is politics/elections-related"""
    ticker   = (market.get("ticker", "") or "").lower()
    title    = (market.get("title", "") or "").lower()
    category = (market.get("category", "") or "").lower()

    politics_keywords = {"election", "president", "senate", "congress", "governor", "mayor", "parliament", "vote", "political", "democrat", "republican"}
    for kw in politics_keywords:
        if kw in ticker or kw in title or kw in category:
            return True
    return False


def is_economics(market: dict) -> bool:
    """Check if market is economics/macro-related"""
    ticker   = (market.get("ticker", "") or "").lower()
    title    = (market.get("title", "") or "").lower()
    category = (market.get("category", "") or "").lower()

    econ_keywords = {"gdp", "inflation", "cpi", "unemployment", "interest rate", "fed", "treasury", "yield", "crypto", "stock", "index", "nasdaq", "s&p", "dow", "macro"}
    for kw in econ_keywords:
        if kw in ticker or kw in title or kw in category:
            return True
    return False


def is_combo(market: dict) -> bool:
    ticker   = (market.get("ticker", "") or "").lower()
    title    = (market.get("title", "") or "").lower()
    category = (market.get("category", "") or "").lower()
    evt      = (market.get("event_ticker", "") or "").lower()

    # Kalshi labels multi-event markets "Multi" — check all identifier fields
    for kw in COMBO_KEYWORDS:
        if kw in ticker or kw in evt or kw in category:
            return True

    # Title-level check (less reliable, avoid false positives)
    if "combo" in title or "parlay" in title:
        return True

    # Two conditions joined by "and"/"&" in the title (any count >= 1 of each)
    and_count = title.count(" and ") + title.count(" & ")
    will_count = title.count("will ")
    if and_count >= 1 and will_count >= 2:  # "Will X ... and will Y ..."
        return True
    if and_count >= 2:  # Double conjunction even without "will"
        return True

    return False


def _cache_category_result(ticker: str, is_crypto_val: bool, is_combo_val: bool, is_sports_val: bool, is_politics_val: bool, is_economics_val: bool):
    """Cache category detection results, with LRU cleanup"""
    if len(_category_cache) >= _CACHE_MAX_SIZE:
        # Remove half the cache if at max (simple LRU)
        for _ in range(_CACHE_MAX_SIZE // 2):
            _category_cache.pop(next(iter(_category_cache)), None)
    _category_cache[ticker] = (is_crypto_val, is_combo_val, is_sports_val, is_politics_val, is_economics_val)


def _is_crypto_cached(market: dict) -> bool:
    """Cached version of is_crypto()"""
    ticker = market.get("ticker", "")
    if ticker in _category_cache:
        return _category_cache[ticker][0]
    result = is_crypto(market)
    _cache_category_result(ticker, result, is_combo(market), is_sports(market), is_politics(market), is_economics(market))
    return result


def _is_combo_cached(market: dict) -> bool:
    """Cached version of is_combo()"""
    ticker = market.get("ticker", "")
    if ticker in _category_cache:
        return _category_cache[ticker][1]
    result = is_combo(market)
    _cache_category_result(ticker, is_crypto(market), result, is_sports(market), is_politics(market), is_economics(market))
    return result


def _is_sports_cached(market: dict) -> bool:
    """Cached version of is_sports()"""
    ticker = market.get("ticker", "")
    if ticker in _category_cache:
        return _category_cache[ticker][2]
    result = is_sports(market)
    _cache_category_result(ticker, is_crypto(market), is_combo(market), result, is_politics(market), is_economics(market))
    return result


def _is_politics_cached(market: dict) -> bool:
    """Cached version of is_politics()"""
    ticker = market.get("ticker", "")
    if ticker in _category_cache:
        return _category_cache[ticker][3]
    result = is_politics(market)
    _cache_category_result(ticker, is_crypto(market), is_combo(market), is_sports(market), result, is_economics(market))
    return result


def _is_economics_cached(market: dict) -> bool:
    """Cached version of is_economics()"""
    ticker = market.get("ticker", "")
    if ticker in _category_cache:
        return _category_cache[ticker][4]
    result = is_economics(market)
    _cache_category_result(ticker, is_crypto(market), is_combo(market), is_sports(market), is_politics(market), result)
    return result


# ---------------------------------------------------------------------------
# Background position monitor — auto-sell on profit target
# ---------------------------------------------------------------------------

def _dollars_to_cents(val) -> float | None:
    """Convert a dollar string like '0.8500' to cents like 85.0"""
    try:
        return round(float(val) * 100, 2)
    except (TypeError, ValueError):
        return None


def _market_price(m: dict, side: str) -> float | None:
    """Get ask price in cents for a side ('yes' or 'no'), trying all known field names."""
    # Try dollar string fields first (e.g. yes_ask_dollars = "0.8500" -> 85)
    v = _dollars_to_cents(m.get(f"{side}_ask_dollars"))
    if v is not None:
        return v
    # Fall back to integer/float cents fields (e.g. yes_ask = 85)
    v = m.get(f"{side}_ask")
    if v is not None:
        try:
            return round(float(v), 2)
        except (TypeError, ValueError):
            pass
    # Some API versions use 'price' on the yes side only
    if side == "yes":
        v = m.get("last_price") or m.get("yes_price")
        if v is not None:
            try:
                f = float(v)
                return round(f * 100 if f <= 1 else f, 2)
            except (TypeError, ValueError):
                pass
    return None


def _market_bid(m: dict, side: str) -> float | None:
    """Get bid price in cents for a side, trying all known field names."""
    v = _dollars_to_cents(m.get(f"{side}_bid_dollars"))
    if v is not None:
        return v
    v = m.get(f"{side}_bid")
    if v is not None:
        try:
            return round(float(v), 2)
        except (TypeError, ValueError):
            pass
    return None


def _monitor():
    while True:
        time.sleep(45)  # 45s — cached market data means no extra Kalshi calls
        with _lock:
            tickers = list(tracked.keys())

        for ticker in tickers:
            with _lock:
                pos = tracked.get(ticker)
                if not pos or pos["status"] != "open":
                    continue
                # Skip only if no profit target exists at all (per-position or global)
                has_pct_target = pos.get("strategy") == "profit" or sell_strategy.get("mode") == "profit"
                has_dol_target = pos.get("target_dollars") is not None or sell_strategy.get("target_dollars") is not None
                if not has_pct_target and not has_dol_target:
                    continue

            try:
                data = kalshi_get(f"/markets/{ticker}")
                m = data.get("market", {})
                bid_d = m.get("yes_bid_dollars") if pos["side"] == "yes" else m.get("no_bid_dollars")
                bid = _dollars_to_cents(bid_d)
                if bid is None:
                    continue

                profit_pct = (bid - pos["buy_price"]) / pos["buy_price"] * 100

                with _lock:
                    if ticker in tracked:
                        tracked[ticker]["current_price"] = bid
                        tracked[ticker]["profit_pct"] = round(profit_pct, 1)

                # Near settlement — let it resolve for full payout; don't try to sell
                if bid >= 97:
                    print(f"[monitor] {ticker} near YES settlement ({bid}¢) — holding to resolution")
                    continue
                if bid <= 3 and pos["side"] == "yes":
                    print(f"[monitor] {ticker} near loss ({bid}¢) — holding, no buyer available")
                    continue

                # Skip auto-sell if market expires soon (per sell_settings)
                if sell_settings.get("skip_auto_sell_near_resolution", False):
                    ct_str = m.get("close_time") or m.get("expiration_time", "")
                    if ct_str:
                        try:
                            ct = datetime.fromisoformat(ct_str.replace("Z", "+00:00"))
                            now = datetime.now(timezone.utc)
                            mins_left = (ct - now).total_seconds() / 60
                            threshold = sell_settings.get("skip_auto_sell_minutes", 1)
                            if mins_left <= threshold:
                                print(f"[monitor] {ticker} expires in {mins_left:.1f} min (< {threshold}) — holding to resolution")
                                continue
                        except Exception:
                            pass

                # Use per-position target if set, otherwise fall back to current global settings
                target_pct     = pos.get("target_pct")     or (sell_strategy.get("target_pct")     if sell_strategy.get("mode") == "profit" else None)
                target_dollars = pos.get("target_dollars") or sell_strategy.get("target_dollars")
                profit_dollars = pos["count"] * (bid - pos["buy_price"]) / 100 if pos.get("buy_price") else None

                hit_pct   = target_pct is not None and profit_pct >= target_pct and (pos.get("strategy") == "profit" or sell_strategy.get("mode") == "profit")
                hit_dol   = target_dollars is not None and profit_dollars is not None and profit_dollars >= target_dollars
                # Target price: sell when bid reaches the specified price in cents
                target_price_c = pos.get("target_price_cents") or sell_strategy.get("target_price_cents")
                hit_price = target_price_c is not None and bid >= target_price_c

                if hit_pct or hit_dol or hit_price:
                    # Build limit sell order with current bid price (Kalshi requires price field)
                    bid_key = "yes_bid_dollars" if pos["side"] == "yes" else "no_bid_dollars"
                    bid_d = m.get(bid_key)
                    if bid_d is None or float(bid_d or 0) < 0.01:
                        print(f"[monitor] {ticker} bid too low to sell ({bid_d}) — skipping")
                        continue
                    price_key = "yes_price_dollars" if pos["side"] == "yes" else "no_price_dollars"
                    result = kalshi_post("/portfolio/orders", {
                        "ticker":   ticker,
                        "action":   "sell",
                        "side":     pos["side"],
                        "type":     "limit",
                        "count":    pos["count"],
                        price_key:  str(bid_d),
                    })
                    with _lock:
                        if ticker in tracked:
                            tracked[ticker]["status"]     = "sold"
                            tracked[ticker]["sold_at"]    = datetime.now(timezone.utc).isoformat()
                            tracked[ticker]["sell_price"] = bid
                            tracked[ticker]["sold_by"]    = "bot_auto"  # auto-sell by strategy
                    _save_tracked()
                    title = pos.get("title", ticker)
                    print(f"[monitor] Auto-sold: {title} | bid={bid}¢ profit={profit_pct:.1f}% / ${profit_dollars:.2f}")

            except Exception as e:
                print(f"[monitor] Error checking {ticker}: {e}")

threading.Thread(target=_monitor, daemon=True).start()

# ---------------------------------------------------------------------------
# Portfolio snapshots — persisted to file, used for PnL time windows
# ---------------------------------------------------------------------------

SNAPSHOTS_FILE = HERE / "portfolio_snapshots.json"
_snap_lock = threading.Lock()
snapshots: list = []   # [{ts: float, v: float}]  v = total dollars

try:
    if SNAPSHOTS_FILE.exists():
        snapshots = json.loads(SNAPSHOTS_FILE.read_text(encoding="utf-8"))
        cutoff = time.time() - 30 * 86400
        snapshots = [s for s in snapshots if s["ts"] >= cutoff]
        print(f"[snapshots] loaded {len(snapshots)} entries")
except Exception as e:
    print(f"[snapshots] load error: {e}")


def _take_snapshot():
    try:
        bal_data = kalshi_get("/portfolio/balance")
        cash_raw = float(bal_data.get("balance") or 0)
        pv_raw   = float(bal_data.get("portfolio_value") or 0)
        cash = round(cash_raw / 100 if cash_raw > 200 else cash_raw, 2)
        pv   = round(pv_raw   / 100 if pv_raw   > 200 else pv_raw,   2)
        total = cash + pv
        with _snap_lock:
            snapshots.append({"ts": time.time(), "v": total})
            cutoff = time.time() - 30 * 86400
            while snapshots and snapshots[0]["ts"] < cutoff:
                snapshots.pop(0)
            SNAPSHOTS_FILE.write_text(json.dumps(snapshots), encoding="utf-8")
    except Exception as e:
        print(f"[snapshots] error: {e}")


def _snapshot_runner():
    _take_snapshot()   # immediate snapshot on startup
    while True:
        time.sleep(300)  # every 5 minutes
        _take_snapshot()

threading.Thread(target=_snapshot_runner, daemon=True).start()

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(HERE, "index.html")


@app.route("/api/debug")
def debug():
    data = kalshi_get("/markets", {"status": "open", "limit": 3})
    return jsonify(data.get("markets", []))

@app.route("/api/debug/balance")
def debug_balance():
    bal = kalshi_get("/portfolio/balance")
    return jsonify(bal)


@app.route("/api/show-public-key")
def show_public_key():
    pub = PRIVATE_KEY.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return pub, 200, {
        "Content-Type": "application/octet-stream",
        "Content-Disposition": "attachment; filename=kalshi_public_key.pem",
    }


@app.route("/api/auth-test")
def auth_test():
    endpoint = "/portfolio/balance"
    full_path = API_PREFIX + endpoint
    url = BASE_URL + full_path
    results = {"key_id_being_used": API_KEY}

    def try_sign(msg_bytes, use_pss=False):
        if isinstance(PRIVATE_KEY, RSAPrivateKey):
            if use_pss:
                return PRIVATE_KEY.sign(msg_bytes, asym_padding.PSS(
                    mgf=asym_padding.MGF1(hashes.SHA256()),
                    salt_length=asym_padding.PSS.MAX_LENGTH), hashes.SHA256())
            return PRIVATE_KEY.sign(msg_bytes, asym_padding.PKCS1v15(), hashes.SHA256())
        return _sign(msg_bytes)

    for label, sign_path, use_pss in [
        ("pkcs1_full",  full_path, False),
        ("pkcs1_short", endpoint,  False),
        ("pss_full",    full_path, True),
        ("pss_short",   endpoint,  True),
    ]:
        try:
            ts = str(int(time.time() * 1000))
            sig = base64.b64encode(try_sign((ts + "GET" + sign_path).encode(), use_pss)).decode()
            hdrs = {"KALSHI-ACCESS-KEY": API_KEY, "KALSHI-ACCESS-TIMESTAMP": ts,
                    "KALSHI-ACCESS-SIGNATURE": sig, "Content-Type": "application/json"}
            r = req.get(url, headers=hdrs, timeout=15)
            results[label] = {"status": r.status_code, "ok": r.ok,
                              "body": r.json() if r.ok else r.text[:200]}
        except Exception as e:
            results[label] = {"error": str(e)}

    return jsonify(results)


@app.route("/api/portfolio")
def portfolio():
    # ── Balance ──
    balance = None
    total_account = None  # total account value (what Kalshi shows as Portfolio)
    try:
        bal_data = kalshi_get("/portfolio/balance")
        # balance_dollars = available CASH only (not total account)
        # portfolio_value = open positions value in CENTS
        cash_dollars = float(bal_data.get("balance_dollars") or 0)
        if not cash_dollars:
            raw = float(bal_data.get("balance") or 0)
            cash_dollars = raw / 100
        pos_cents   = float(bal_data.get("portfolio_value") or 0)
        pos_dollars = round(pos_cents / 100, 2)  # always /100
        balance       = round(cash_dollars, 2)                    # cash = balance_dollars directly
        total_account = round(cash_dollars + pos_dollars, 2)      # total = cash + positions
        print(f"[portfolio] cash=${cash_dollars:.2f} positions=${pos_dollars:.2f} → total=${total_account:.2f}")
    except Exception as e:
        print(f"[portfolio] balance error: {e}")

    # ── Positions ──
    positions = []
    portfolio_value = 0.0
    try:
        # Fetch ALL open positions with cursor pagination
        raw_positions = []
        cursor = None
        pages = 0
        while pages < 5:  # max 5 pages × 200 = 1000 positions
            params = {"count": 200}
            if cursor: params["cursor"] = cursor
            pos_data = kalshi_get("/portfolio/positions", params)
            batch = pos_data.get("market_positions", pos_data.get("positions", []))
            raw_positions.extend(batch)
            cursor = pos_data.get("cursor")
            pages += 1
            if not cursor or len(batch) < 200:
                break

        for p in raw_positions:
            ticker = p.get("market_id") or p.get("ticker", "")
            # Kalshi switched to `position_fp` (string float, signed: +yes / -no).
            # Fall back to older int fields for safety.
            qty_raw = p.get("position_fp", p.get("position", p.get("quantity_owned", 0)))
            try:
                qty = float(qty_raw)
            except (TypeError, ValueError):
                qty = 0
            if abs(qty) < 0.001:
                continue

            # Dollar-string fields → cents (legacy frontend expects cents/int)
            def _d2c(v):
                try: return round(float(v) * 100, 2)
                except (TypeError, ValueError): return 0
            total_traded = _d2c(p.get("total_traded_dollars")) if "total_traded_dollars" in p else p.get("total_traded", 0)
            realized_pnl = _d2c(p.get("realized_pnl_dollars")) if "realized_pnl_dollars" in p else p.get("realized_pnl", 0)

            market_title = ticker
            event_ticker = ""
            category     = ""
            current_yes  = None
            current_no   = None
            close_time   = None
            time.sleep(0.15)  # rate limit: 20 positions × 0.15s = 3s max, avoids 429
            try:
                mkt          = _get_market(ticker)
                event_ticker = mkt.get("event_ticker", "")
                market_title = _event_title(event_ticker) or mkt.get("title", ticker)
                category     = mkt.get("category", "")
                current_yes  = _dollars_to_cents(mkt.get("yes_bid_dollars"))
                current_no   = _dollars_to_cents(mkt.get("no_bid_dollars"))
                close_time   = mkt.get("close_time") or mkt.get("expiration_time")
            except Exception:
                pass

            # Portfolio value = contracts * current bid price
            side = "yes" if qty > 0 else "no"
            bid  = current_yes if side == "yes" else current_no
            if bid:
                portfolio_value += abs(qty) * bid / 100

            bot_info = tracked.get(ticker)

            # Derive average buy price from total_traded_dollars for non-bot positions
            # Cap at 99¢ — if derived price > 99 it means total_traded includes
            # multiple round-trips and isn't a reliable cost basis (show — instead)
            derived_buy_price = None
            if not bot_info:
                ttd = float(p.get("total_traded_dollars") or 0)
                if ttd > 0 and abs(qty) > 0.001:
                    raw = round(ttd / abs(qty) * 100)
                    derived_buy_price = raw if raw <= 99 else None

            buy_price = (bot_info["buy_price"] if bot_info else None) or derived_buy_price

            positions.append({
                "ticker":         ticker,
                "event_ticker":   event_ticker,
                "title":          market_title,
                "category":       category,
                "kalshi_url":     _kalshi_url(event_ticker, ticker),
                "quantity":       qty,
                "total_traded":   total_traded,
                "realized_pnl":   realized_pnl,
                "resting_orders": p.get("resting_orders_count", 0),
                "current_yes":    current_yes,
                "current_no":     current_no,
                "close_time":     close_time,
                "bot_bought":     bot_info is not None,
                "buy_price":      buy_price,
                "strategy":       bot_info.get("strategy") if bot_info else None,
                "target_pct":     bot_info.get("target_pct") if bot_info else None,
                "bought_at":      bot_info.get("bought_at") if bot_info else None,
            })
    except Exception as e:
        print(f"[portfolio] positions error: {e}")

    # ── Tracked fallback ──────────────────────────────────────────────────────
    # Merge in any bot-tracked "open" positions not returned by Kalshi's live API.
    # This happens right after a buy (Kalshi delay) or when the positions API errors.
    live_tickers = {p["ticker"] for p in positions}
    with _lock:
        tracked_snap = {k: dict(v) for k, v in tracked.items()}

    for ticker, info in tracked_snap.items():
        if ticker in live_tickers or info.get("status") != "open":
            continue
        event_ticker = ""
        market_title = info.get("title", ticker)
        category     = ""
        current_yes  = None
        current_no   = None
        try:
            mkt        = _get_market(ticker)
            mkt_status = (mkt.get("status") or "").lower()
            if mkt_status in ("settled", "resolved", "finalized", "closed"):
                with _lock:
                    if ticker in tracked:
                        tracked[ticker]["status"] = "sold"
                _save_tracked()
                continue
            event_ticker = mkt.get("event_ticker", "")
            market_title = _event_title(event_ticker) or mkt.get("title", ticker)
            category     = mkt.get("category", "")
            current_yes  = _dollars_to_cents(mkt.get("yes_bid_dollars"))
            current_no   = _dollars_to_cents(mkt.get("no_bid_dollars"))
        except Exception:
            pass

        side = info.get("side", "yes")
        count = info.get("count", 0)
        qty   = count if side == "yes" else -count

        bid = current_yes if side == "yes" else current_no
        if bid:
            portfolio_value += count * bid / 100

        positions.append({
            "ticker":         ticker,
            "event_ticker":   event_ticker,
            "title":          market_title,
            "category":       category,
            "kalshi_url":     _kalshi_url(event_ticker, ticker),
            "quantity":       qty,
            "total_traded":   0,
            "realized_pnl":   0,
            "resting_orders": 0,
            "current_yes":    current_yes,
            "current_no":     current_no,
            "bot_bought":     True,
            "buy_price":      info.get("buy_price"),
            "strategy":       info.get("strategy"),
            "target_pct":     info.get("target_pct"),
            "bought_at":      info.get("bought_at"),
        })
        pass  # suppress repeated fallback log spam

    # Use Kalshi's own portfolio_value (open positions value) from the balance endpoint
    # as fallback when position-by-position calculation returns 0
    api_positions_value = 0.0
    try:
        pv_raw = float(bal_data.get("portfolio_value", 0))
        if pv_raw > 0:
            api_positions_value = round(pv_raw / 100 if pv_raw > 200 else pv_raw, 2)
    except Exception:
        pass

    if portfolio_value == 0.0:
        portfolio_value = api_positions_value

    total_value = total_account if total_account is not None else round((balance or 0) + portfolio_value, 2)

    settle_hours = int(request.args.get("settlement_hours", 24))
    recent_settlements = _cached_settlements(hours=settle_hours)

    return jsonify({
        "balance":            balance,           # spendable cash
        "positions_value":    round(portfolio_value, 2),  # open positions value only
        "portfolio_value":    total_value,       # total account = cash + positions (matches Kalshi)
        "positions":          positions,
        "recent_settlements": recent_settlements,
    })


def _recent_settlements(hours: int = 24) -> list:
    """Fetch Kalshi settlements from the past `hours` hours, enriched with title/category."""
    cutoff_ts = datetime.now(timezone.utc) - timedelta(hours=hours)
    out = []
    cursor = None
    pages = 0
    try:
        while pages < 10:
            params = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            data = kalshi_get("/portfolio/settlements", params)
            batch = data.get("settlements", [])
            if not batch:
                break
            stop = False
            for s in batch:
                ts_str = s.get("settled_time", "")
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if ts < cutoff_ts:
                    stop = True
                    break

                ticker = s.get("ticker", "")
                evt    = s.get("event_ticker", "")
                title  = _event_title(evt) or ticker
                category = ""
                try:
                    mkt = _get_market(ticker)
                    title    = title or mkt.get("title", ticker)
                    category = mkt.get("category", "")
                except Exception:
                    pass

                yes_cnt  = float(s.get("yes_count_fp") or 0)
                no_cnt   = float(s.get("no_count_fp")  or 0)
                yes_cost = float(s.get("yes_total_cost_dollars") or 0)
                no_cost  = float(s.get("no_total_cost_dollars")  or 0)
                revenue  = float(s.get("revenue") or 0) / 100   # cents → dollars
                result   = s.get("market_result", "")
                count    = yes_cnt if yes_cnt > 0.001 else no_cnt
                # Calculate fee ourselves: $0.01/contract only on winning settlements
                fee = round(count * 0.01, 4) if revenue > 0.001 else 0

                # Skip entries with no actual position (combo placeholders, settled-zero rows)
                if yes_cnt < 0.001 and no_cnt < 0.001 and yes_cost < 0.001 and no_cost < 0.001:
                    continue

                # Skip hedged positions (bought both sides → net zero, closed pre-settlement).
                # Kalshi records these as settlements with revenue=0; the realized PnL was
                # taken when the offsetting trade was made, not at settlement.
                if yes_cnt > 0.001 and no_cnt > 0.001:
                    continue

                if yes_cnt > 0.001:
                    side, count, cost = "yes", yes_cnt, yes_cost
                else:
                    side, count, cost = "no", no_cnt, no_cost

                pnl = round(revenue - cost - fee, 4)

                out.append({
                    "ticker":       ticker,
                    "event_ticker": evt,
                    "title":        title,
                    "category":     category,
                    "kalshi_url":   _kalshi_url(evt, ticker),
                    "side":         side,
                    "count":        round(count, 2),
                    "cost":         round(cost, 2),
                    "revenue":      round(revenue, 2),
                    "pnl":          pnl,
                    "result":       result,
                    "settled_time": ts_str,
                    "won":          pnl > 0.001,
                    "sold_by":      _get_sold_by(ticker),
                })
            if stop:
                break
            cursor = data.get("cursor")
            if not cursor:
                break
            pages += 1
    except Exception as e:
        print(f"[settlements] error: {e}")

    # Also include bot-sold positions (early exits) not in Kalshi settlements
    kalshi_tickers = {r["ticker"] for r in out}
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with _lock:
        tracked_snap = {k: dict(v) for k, v in tracked.items()}
    for tkr, pos in tracked_snap.items():
        if pos.get("status") != "sold": continue
        if tkr in kalshi_tickers: continue  # already in settlements
        sold_at = pos.get("sold_at") or pos.get("bought_at", "")
        if not sold_at or sold_at < cutoff_iso: continue
        bp    = pos.get("buy_price", 0) or 0
        sp    = pos.get("sell_price", 0) or 0
        count = pos.get("count", 0) or 0
        cost  = round(count * bp / 100, 4)
        revenue = round(count * sp / 100, 4) if sp else 0
        fee   = round(count * 0.01, 4) if revenue > 0 else 0
        pnl   = round(revenue - cost - fee, 4)
        evt   = tkr.rsplit("-", 1)[0] if "-" in tkr else ""
        out.append({
            "ticker":       tkr,
            "event_ticker": evt,
            "title":        pos.get("title", tkr),
            "category":     pos.get("category", ""),
            "kalshi_url":   _kalshi_url(evt, tkr),
            "side":         pos.get("side", "yes"),
            "count":        round(count, 2),
            "cost":         round(cost, 2),
            "revenue":      round(revenue, 2),
            "pnl":          pnl,
            "result":       "sold_early",
            "settled_time": sold_at,
            "won":          pnl > 0.001,
            "sold_by":      _get_sold_by(tkr),
        })

    # Sort combined list by settled_time descending
    out.sort(key=lambda r: r.get("settled_time",""), reverse=True)
    return out


@app.route("/api/debug_positions")
def debug_positions():
    out = {}
    probes = [
        ("positions",       "/portfolio/positions",       {"count": 200}),
        ("positions_all",   "/portfolio/positions",       {"count": 200, "settlement_status": "all"}),
        ("orders_resting",  "/portfolio/orders",          {"status": "resting", "limit": 100}),
        ("orders_all",      "/portfolio/orders",          {"limit": 100}),
        ("fills",           "/portfolio/fills",           {"limit": 100}),
        ("settlements",     "/portfolio/settlements",     {"limit": 100}),
        ("balance",         "/portfolio/balance",         None),
        ("rest_positions",  "/portfolio/resting_orders",  None),
    ]
    for label, ep, params in probes:
        try:
            out[label] = kalshi_get(ep, params)
        except Exception as e:
            out[label] = {"_error": str(e)}
    return jsonify(out)


@app.route("/api/debug_events")
def debug_events():
    """Return first page of /events so we can see the structure and event_tickers."""
    try:
        data = kalshi_get("/events", {"status": "open", "limit": 50})
        events = data.get("events", [])
        rows = []
        for e in events[:50]:
            et = e.get("event_ticker", "")
            rows.append({
                "event_ticker": et,
                "is_kxmv": et.upper().startswith("KXMV"),
                "title": e.get("title", "")[:80],
                "category": e.get("category", ""),
                "series_ticker": e.get("series_ticker", ""),
                "close_time": e.get("close_time", ""),
                "status": e.get("status", ""),
                "keys": list(e.keys()),
            })
        return jsonify({"count": len(rows), "events": rows})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/debug_series")
def debug_series():
    """List all series + test timestamp-filtered market query."""
    result = {}

    # 1. Try /series endpoint
    try:
        data = kalshi_get("/series", {"limit": 200})
        all_series = data.get("series", [])
        result["series_count"] = len(all_series)
        result["series_sample"] = [
            {
                "series_ticker": s.get("series_ticker", ""),
                "title": s.get("title", "")[:60],
                "category": s.get("category", ""),
                "frequency": s.get("frequency", ""),
                "keys": list(s.keys()),
            }
            for s in all_series[:30]
        ]
        result["series_error"] = None
    except Exception as e:
        result["series_error"] = str(e)
        result["series_count"] = 0
        result["series_sample"] = []

    # 2. Test timestamp-filtered market query
    now    = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=24)
    try:
        ts_data = kalshi_get("/markets", {
            "status": "open",
            "limit": 20,
            "min_close_ts": int(now.timestamp()),
            "max_close_ts": int(cutoff.timestamp()),
        })
        ts_markets = ts_data.get("markets", [])
        result["ts_filter_count"] = len(ts_markets)
        result["ts_filter_sample"] = [
            {
                "ticker": m.get("ticker", ""),
                "close_time": m.get("close_time", ""),
                "yes_ask_dollars": m.get("yes_ask_dollars"),
                "no_ask_dollars": m.get("no_ask_dollars"),
                "is_kxmv": (m.get("ticker", "") or "").upper().startswith("KXMV"),
            }
            for m in ts_markets[:20]
        ]
        result["ts_filter_error"] = None
    except Exception as e:
        result["ts_filter_error"] = str(e)
        result["ts_filter_count"] = 0
        result["ts_filter_sample"] = []

    return jsonify(result)


@app.route("/api/debug_scan")
def debug_scan():
    """Combined debug: 60-min market window + series probe + timestamp filter test."""
    now    = datetime.now(timezone.utc)
    cutoff = now + timedelta(minutes=60)

    # ── Part 1: flat-pagination window markets ────────────────────────────────
    window_markets = []
    cursor = None
    pages  = 0
    try:
        while pages < 8:
            params = {"status": "open", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            data  = kalshi_get("/markets", params)
            batch = data.get("markets", [])
            pages += 1
            for m in batch:
                ct_str = m.get("close_time") or m.get("expiration_time", "")
                if not ct_str:
                    continue
                try:
                    ct = datetime.fromisoformat(ct_str.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if not (now < ct <= cutoff):
                    continue
                yes_ask = _market_price(m, "yes")
                no_ask  = _market_price(m, "no")
                oi      = (m.get("open_interest_fp") or m.get("open_interest") or
                          m.get("volume_fp") or m.get("volume") or m.get("volume_24h_fp") or 0)
                secs    = max(0, int((ct - now).total_seconds()))
                window_markets.append({
                    "ticker":    m.get("ticker"),
                    "title":     m.get("title","")[:60],
                    "category":  m.get("category"),
                    "yes_ask":   yes_ask,
                    "no_ask":    no_ask,
                    "yes_ask_dollars_raw": m.get("yes_ask_dollars"),
                    "no_ask_dollars_raw":  m.get("no_ask_dollars"),
                    "open_interest_fp": m.get("open_interest_fp"),
                    "volume_fp": m.get("volume_fp"),
                    "oi_used":   oi,
                    "secs_left": secs,
                    "status":    m.get("status"),
                    "is_crypto": is_crypto(m),
                    "is_combo":  is_combo(m),
                    "all_keys":  list(m.keys()),
                })
            cursor = data.get("cursor")
            if not cursor or not batch:
                break
    except Exception as e:
        pass

    # ── Part 2: timestamp-filtered market test ────────────────────────────────
    ts_result = {}
    try:
        ts_data = kalshi_get("/markets", {
            "status": "open", "limit": 20,
            "min_close_ts": int(now.timestamp()),
            "max_close_ts": int((now + timedelta(hours=24)).timestamp()),
        })
        ts_mkts = ts_data.get("markets", [])
        ts_result = {
            "count": len(ts_mkts),
            "non_kxmv": sum(1 for m in ts_mkts if not (m.get("ticker","") or "").upper().startswith("KXMV")),
            "sample": [
                {"ticker": m.get("ticker",""), "close_time": m.get("close_time",""),
                 "yes_ask": m.get("yes_ask_dollars"), "no_ask": m.get("no_ask_dollars"),
                 "is_kxmv": (m.get("ticker","") or "").upper().startswith("KXMV")}
                for m in ts_mkts[:10]
            ],
        }
    except Exception as e:
        ts_result = {"error": str(e)}

    # ── Part 3: known series probe ────────────────────────────────────────────
    PROBE = ["KXBTC","KXBTCD","KXBTCW","KXETH","KXETHUSD","KXSOL",
             "INX","INXD","KXNDAQ","NDX","KXFED","KXCPI",
             "NBAG","KXNBA","MLBG","KXMLB","NFLG","KXNFL","KXNHL"]
    series_hits = []
    for st in PROBE:
        try:
            time.sleep(0.3)
            d = kalshi_get("/markets", {"series_ticker": st, "status": "open", "limit": 5})
            mkts = d.get("markets", [])
            if mkts:
                m0 = mkts[0]
                series_hits.append({
                    "series": st,
                    "count": len(mkts),
                    "sample_ticker": m0.get("ticker",""),
                    "sample_close": m0.get("close_time",""),
                    "yes_ask": m0.get("yes_ask_dollars"),
                    "no_ask": m0.get("no_ask_dollars"),
                })
        except Exception:
            pass

    return jsonify({
        "window_60min": {"count": len(window_markets), "markets": window_markets[:30]},
        "ts_filter_24h": ts_result,
        "series_probe":  series_hits,
    })


def _apply_market_filters(m, now, cutoff, min_thr, max_thr,
                          no_crypto, no_combo, no_sports, no_politics, no_economics, good_liq, min_open_int,
                          min_age_mins=None, max_age_mins=None, no_buy_within_mins=None,
                          crypto_times=None, hide_multi=False):
    """
    Run all scan filters on a single market dict.
    Returns (side, price, secs_left) on pass, or None on reject.
    Prints a reason line when rejecting.
    """
    ticker = m.get("ticker", "?")

    if ticker.upper().startswith("KXMV"):
        return None

    ct_str = m.get("close_time") or m.get("expiration_time", "")
    if not ct_str:
        return None
    try:
        ct = datetime.fromisoformat(ct_str.replace("Z", "+00:00"))
    except ValueError:
        return None
    if not (now < ct <= cutoff):
        return None

    # Open-time filtering (if requested)
    if min_age_mins is not None or max_age_mins is not None:
        ot_str = m.get("open_time") or m.get("opened_time") or m.get("created_time") or ""
        if ot_str:
            try:
                ot = datetime.fromisoformat(ot_str.replace("Z", "+00:00"))
                age_mins = (now - ot).total_seconds() / 60
                if min_age_mins is not None and age_mins < min_age_mins:
                    return None
                if max_age_mins is not None and age_mins > max_age_mins:
                    return None
            except ValueError:
                pass

    # Category filtering: show ONLY the selected categories
    is_m_crypto = _is_crypto_cached(m)
    is_m_combo = _is_combo_cached(m)
    is_m_sports = _is_sports_cached(m)
    is_m_politics = _is_politics_cached(m)
    is_m_economics = _is_economics_cached(m)

    # Build set of allowed categories (those NOT excluded)
    allowed = set()
    if not no_crypto:
        allowed.add("crypto")
    if not no_combo:
        allowed.add("combo")
    if not no_sports:
        allowed.add("sports")
    if not no_politics:
        allowed.add("politics")
    if not no_economics:
        allowed.add("economics")

    # If nothing is allowed, reject everything
    if not allowed:
        return None

    # Reject if market doesn't match ANY allowed category
    matches = (is_m_crypto and "crypto" in allowed) or \
              (is_m_combo and "combo" in allowed) or \
              (is_m_sports and "sports" in allowed) or \
              (is_m_politics and "politics" in allowed) or \
              (is_m_economics and "economics" in allowed)

    if not matches:
        if DEBUG_LOGGING:
            if is_m_crypto:
                print(f"[scan] crypto (not selected): {ticker}")
            elif is_m_combo:
                print(f"[scan] combo (not selected): {ticker}")
            elif is_m_sports:
                print(f"[scan] sports (not selected): {ticker}")
            elif is_m_politics:
                print(f"[scan] politics (not selected): {ticker}")
            elif is_m_economics:
                print(f"[scan] economics (not selected): {ticker}")
            else:
                print(f"[scan] other (not selected): {ticker}")
        return None
    if good_liq:
        oi = (m.get("open_interest_fp") or m.get("open_interest") or
              m.get("volume_fp") or m.get("volume") or m.get("volume_24h_fp") or 0)
        if float(oi) < min_open_int:
            if DEBUG_LOGGING:
                print(f"[scan] thin: {ticker} oi_fp={m.get('open_interest_fp')} vol_fp={m.get('volume_fp')}")
            return None

    yes_ask = _market_price(m, "yes")
    no_ask  = _market_price(m, "no")
    side = price = None
    if yes_ask is not None and min_thr <= yes_ask < max_thr:
        side, price = "yes", yes_ask
    elif no_ask is not None and min_thr <= no_ask < max_thr:
        side, price = "no", no_ask
    if side is None:
        if DEBUG_LOGGING:
            print(f"[scan] price: {ticker} yes={yes_ask} no={no_ask} want {min_thr}–{max_thr}")
        return None

    # Live-market guard: always require a real bid on the trading side.
    # A market with no bid is not actually tradeable — you couldn't sell it
    # back, and it's usually a sign of a dead/illiquid listing.
    bid_c = _market_bid(m, side)
    if bid_c is None or bid_c < 1:
        if DEBUG_LOGGING:
            print(f"[scan] no-bid (not live): {ticker} side={side} bid={bid_c}")
        return None
    # Status check — only show truly open/active markets
    mkt_status = (m.get("status") or "").lower()
    if mkt_status and mkt_status not in ("open", "active", "live"):
        if DEBUG_LOGGING:
            print(f"[scan] status not live: {ticker} status={mkt_status}")
        return None
    # Spread check stays gated behind good_liq (strict filtering toggle)
    if good_liq and (price - bid_c) > 8:
        if DEBUG_LOGGING:
            print(f"[scan] spread: {ticker} ask={price} bid={bid_c}")
        return None

    secs_left = max(0, int((ct - now).total_seconds()))

    # "Don't buy if within X minutes of end"
    if no_buy_within_mins is not None and secs_left < no_buy_within_mins * 60:
        return None

    tkr = m.get("ticker", "")

    # Crypto time period filter
    if not no_crypto and is_m_crypto and crypto_times is not None:
        if "15m" not in crypto_times and "15M" in tkr.upper():
            return None
        if "30m" not in crypto_times and "30M" in tkr.upper():
            return None
        if "1h" not in crypto_times and "1H" in tkr.upper():
            return None
        # Daily/range = has price-level suffix (B73500, T73999.99)
        import re as _re
        is_level = bool(_re.search(r'-[BT]\d', tkr))
        if is_level and "daily" not in crypto_times:
            return None
        if not is_level and "15M" not in tkr.upper() and "30M" not in tkr.upper() and "1H" not in tkr.upper() and "weekly" not in crypto_times:
            return None

    # Hide multi-outcome: skip markets with price-level suffixes (B73500, T73999.99)
    if hide_multi:
        import re as _re
        if _re.search(r'-[BT]\d', tkr):
            return None

    return side, price, secs_left


def _market_to_result(m, side, price, secs_left):
    evt = m.get("event_ticker", "")
    etitle = _event_title(evt) or m.get("title", m.get("ticker", ""))
    return {
        "ticker":       m.get("ticker", ""),
        "event_ticker": evt,
        "title":        etitle,
        "market_q":     m.get("title", ""),
        "category":     m.get("category", ""),
        "kalshi_url":   _kalshi_url(evt, m.get("ticker","")),
        "side":         side,
        "price":        price,
        "yes_ask":      _market_price(m, "yes"),
        "no_ask":       _market_price(m, "no"),
        "secs_left":    secs_left,
    }


@app.route("/api/scan")
def scan():
    try:
        min_thr        = float(request.args.get("min_thr", 85))
        max_thr        = float(request.args.get("max_thr", 96))
        minutes        = int(request.args.get("minutes", 15))
        show_crypto    = request.args.get("show_crypto", "false").lower() == "true"
        show_combo     = request.args.get("show_combo", "false").lower() == "true"
        show_sports    = request.args.get("show_sports", "false").lower() == "true"
        show_politics  = request.args.get("show_politics", "false").lower() == "true"
        show_economics = request.args.get("show_economics", "false").lower() == "true"
        good_liq       = request.args.get("good_liq", "false").lower() == "true"
        min_open_int   = 10
        min_age_raw    = request.args.get("min_age_mins", "")
        max_age_raw    = request.args.get("max_age_mins", "")
        min_age_mins   = float(min_age_raw) if min_age_raw else None
        max_age_mins   = float(max_age_raw) if max_age_raw else None
        no_buy_within_raw = request.args.get("no_buy_within_mins", "")
        no_buy_within_mins = float(no_buy_within_raw) if no_buy_within_raw else None
        crypto_times_raw = request.args.get("crypto_times", "15m,30m,1h,daily,weekly")
        crypto_times = set(crypto_times_raw.split(",")) if crypto_times_raw else {"15m","30m","1h","daily","weekly"}
        hide_multi = request.args.get("hide_multi", "false").lower() == "true"

        # Convert "show" logic to "exclude" logic for the filter function
        no_crypto    = not show_crypto
        no_combo     = not show_combo
        no_sports    = not show_sports
        no_politics  = not show_politics
        no_economics = not show_economics
    except ValueError:
        return jsonify({"error": "Invalid parameters"}), 400

    now    = datetime.now(timezone.utc)
    cutoff = now + timedelta(minutes=minutes)
    results = []

    # If "buy at X¢, sell at Y¢" strategy active, override max_thr with buy-in price
    bip = sell_strategy.get("buy_in_price_cents")
    if bip is not None and sell_strategy.get("mode") == "profit":
        max_thr = min(max_thr, float(bip))

    def _scan_batch(markets_iter):
        for m in markets_iter:
            hit = _apply_market_filters(m, now, cutoff, min_thr, max_thr,
                                        no_crypto, no_combo, no_sports, no_politics, no_economics, good_liq, min_open_int,
                                        min_age_mins=min_age_mins, max_age_mins=max_age_mins,
                                        no_buy_within_mins=no_buy_within_mins,
                                        crypto_times=crypto_times, hide_multi=hide_multi)
            if hit:
                results.append(_market_to_result(m, *hit))
            if len(results) >= 20:
                break

    def _rate_get(endpoint, params):
        time.sleep(0.35)
        return kalshi_get(endpoint, params)

    # ── Approach 1: direct probe of known short-term series (fastest) ─────────
    # These bypass KXMV completely; each is a direct series query
    KNOWN_SERIES = [
        # Crypto (daily/weekly ranges)
        "KXBTC", "KXBTCD", "KXBTCW", "KXBTCM",
        "KXETH", "KXETHUSD", "KXETHD",
        "KXSOL", "KXSOLD", "KXDOGE", "KXXRP",
        # US indices
        "INX", "INXD", "INXW",
        "KXNDAQ", "KXNDAQD", "NDX",
        "KXDJIA", "DJI",
        # Fed / macro
        "KXFED", "KXCPI", "KXPCE", "KXUNEMP",
        # Sports (game-by-game)
        "NBAG", "KXNBA", "NBA",
        "MLBG", "KXMLB", "MLB",
        "NFLG", "KXNFL", "NFL",
        "KXNHL", "NHL",
        "KXSOCCER",
    ]
    found_any = False
    for st in KNOWN_SERIES:
        if len(results) >= 20:
            break
        try:
            d = _rate_get("/markets", {"series_ticker": st, "status": "open", "limit": 200})
            batch = d.get("markets", [])
            if batch:
                if DEBUG_LOGGING:
                    print(f"[scan] series {st}: {len(batch)} markets")
                found_any = True
                _scan_batch(batch)
        except Exception:
            pass
    if found_any:
        if DEBUG_LOGGING:
            print(f"[scan] probe phase done: {len(results)} results")

    # ── Approach 2: timestamp-filtered (if API supports it) ───────────────────
    if not results:
        try:
            ts_params = {
                "status": "open", "limit": 200,
                "min_close_ts": int(now.timestamp()),
                "max_close_ts": int(cutoff.timestamp()),
            }
            ts_data    = kalshi_get("/markets", ts_params)
            ts_markets = ts_data.get("markets", [])
            non_kxmv   = [m for m in ts_markets
                          if not (m.get("ticker","") or "").upper().startswith("KXMV")]
            if DEBUG_LOGGING:
                print(f"[scan] ts-filter: {len(ts_markets)} total, {len(non_kxmv)} non-KXMV")
            if non_kxmv:
                _scan_batch(ts_markets)
                ts_cursor = ts_data.get("cursor")
                ts_pages  = 1
                while len(results) < 20 and ts_cursor and ts_pages < 30:
                    pg = _rate_get("/markets", {**ts_params, "cursor": ts_cursor})
                    _scan_batch(pg.get("markets", []))
                    ts_cursor = pg.get("cursor")
                    ts_pages += 1
                    if not pg.get("markets"):
                        break
        except Exception as e:
            if DEBUG_LOGGING:
                print(f"[scan] ts-filter error: {e}")

    # ── Approach 3: /series list → query each ────────────────────────────────
    if not results:
        try:
            sr = kalshi_get("/series", {"limit": 200})
            series_tickers = [
                s.get("series_ticker", "") for s in sr.get("series", [])
                if s.get("series_ticker", "")
                and not s.get("series_ticker","").upper().startswith("KXMV")
                and s.get("series_ticker","") not in KNOWN_SERIES
            ]
            if DEBUG_LOGGING:
                print(f"[scan] /series list: {len(series_tickers)} additional series")
            for st in series_tickers:
                if len(results) >= 20:
                    break
                try:
                    d = _rate_get("/markets", {"series_ticker": st, "status": "open", "limit": 200})
                    _scan_batch(d.get("markets", []))
                except Exception:
                    pass
        except Exception as e:
            if DEBUG_LOGGING:
                print(f"[scan] /series error: {e}")

    # ── Approach 4: flat pagination fallback ─────────────────────────────────
    if not results:
        print("[scan] flat pagination fallback")
        cursor = None
        pages  = 0
        try:
            while len(results) < 20 and pages < 100:
                params = {"status": "open", "limit": 200}
                if cursor:
                    params["cursor"] = cursor
                data  = kalshi_get("/markets", params)
                batch = data.get("markets", [])
                pages += 1
                _scan_batch(batch)
                cursor = data.get("cursor")
                if not cursor or not batch:
                    break
        except req.HTTPError as e:
            return jsonify({"error": f"Kalshi API: {e.response.status_code}"}), 502
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── Log scan run ──────────────────────────────────────────────────────────
    try:
        entry = json.dumps({
            "ts":      datetime.now(timezone.utc).isoformat(),
            "filters": {"min": min_thr, "max": max_thr, "mins": minutes},
            "found":   [{"ticker": r["ticker"], "price": r["price"],
                         "side": r["side"], "cat": r["category"]} for r in results],
        })
        with open(SCAN_LOG, "a", encoding="utf-8") as f:
            f.write(entry + "\n")
    except Exception:
        pass

    return jsonify(results)


@app.route("/api/buy", methods=["POST"])
def buy():
    data     = request.get_json(silent=True) or {}
    ticker   = data.get("ticker", "")
    side     = data.get("side", "")
    dollars  = float(data.get("amount", 0))
    price_c  = int(data.get("price", 0))
    title    = data.get("title", ticker)
    category = data.get("category", "")
    override = data.get("override", False)

    if not ticker or side not in ("yes", "no") or dollars <= 0 or price_c <= 0:
        return jsonify({"error": "Missing or invalid fields"}), 400

    # Server-side hard cap — clamp anything above the cap rather than refuse.
    # Backstop against runaway client sizing bugs (e.g. the historical 20-contract
    # ETH blowup at -$18.86) without ever telling the user "no" in the UI.
    HARD_CAP = float(os.environ.get("KALSHI_MAX_PER_MARKET", "5.00"))
    if dollars > HARD_CAP:
        print(f"[buy] clamped ${dollars:.2f} -> ${HARD_CAP:.2f} cap on {ticker}")
        dollars = HARD_CAP

    # Check per-ticker buy count against max_per_market setting
    max_per = int(data.get("max_per_market", 1) or 1)
    with _lock:
        existing = tracked.get(ticker)
        # Count how many times this ticker has been bought and is still open
        open_count = sum(1 for t, p in tracked.items()
                         if t == ticker and p.get("status") == "open")
    if open_count >= max_per and not override:
        if max_per <= 1:
            return jsonify({"error": f"Already holding {ticker} — increase 'Max buys per contract type' or sell first", "can_override": True}), 409
        else:
            return jsonify({"error": f"Already have {open_count}/{max_per} of {ticker}", "can_override": True}), 409

    contracts = math.floor(dollars / (price_c / 100))
    if contracts < 1:
        return jsonify({"error": f"${dollars:.2f} can't buy 1 contract at {price_c}¢"}), 400

    # Build order — client_order_id required by Kalshi; include price as worst-case fill
    order_body = {
        "ticker":          ticker,
        "client_order_id": str(uuid.uuid4()),
        "action":          "buy",
        "side":            side,
        "type":            "market",
        "count":           contracts,
    }
    # Add worst-case price field so Kalshi accepts the market order
    if side == "yes":
        order_body["yes_price"] = int(math.ceil(price_c))
    else:
        order_body["no_price"] = int(math.ceil(price_c))

    try:
        result = kalshi_post("/portfolio/orders", order_body)
    except req.HTTPError as e:
        return jsonify({"error": f"Order failed ({e.response.status_code}): {e.response.text[:200]}"}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    order = result.get("order", result)

    with _lock:
        strat = sell_strategy.copy()
        tracked[ticker] = {
            "title":         title,
            "category":      category,
            "side":          side,
            "count":         contracts,
            "buy_price":     price_c,
            "current_price": price_c,
            "profit_pct":    0.0,
            "strategy":           strat["mode"],
            "target_pct":         strat.get("target_pct"),
            "target_dollars":     strat.get("target_dollars"),
            "target_price_cents": strat.get("target_price_cents"),
            "status":        "open",
            "bought_at":     datetime.now(timezone.utc).isoformat(),
        }
    _save_tracked()

    return jsonify({
        "ok":        True,
        "order_id":  order.get("order_id"),
        "status":    order.get("status"),
        "contracts": contracts,
        "ticker":    ticker,
        "side":      side.upper(),
        "spent":     round(contracts * price_c / 100, 2),
    })


@app.route("/api/sell", methods=["POST"])
def sell():
    data    = request.get_json(silent=True) or {}
    ticker  = data.get("ticker", "")
    side    = data.get("side", "")
    count   = int(data.get("count", 0))

    if not ticker or side not in ("yes", "no") or count < 1:
        return jsonify({"error": "Invalid fields"}), 400

    try:
        # Fetch current bid to include as price (Kalshi requires it)
        mkt_data = _get_market(ticker)
        if side == "yes":
            bid_d = mkt_data.get("yes_bid_dollars") or mkt_data.get("yes_ask_dollars")
        else:
            bid_d = mkt_data.get("no_bid_dollars") or mkt_data.get("no_ask_dollars")

        bid_cents = round(float(bid_d or 0) * 100)
        if bid_cents < 1:
            return jsonify({"error": f"Cannot sell — current bid is 0¢ (market likely already resolved or no buyers). Check Kalshi directly."}), 400

        order_payload = {
            "ticker": ticker,
            "action": "sell",
            "side":   side,
            "type":   "limit",
            "count":  count,
        }
        price_key = "yes_price_dollars" if side == "yes" else "no_price_dollars"
        order_payload[price_key] = str(bid_d)

        result = kalshi_post("/portfolio/orders", order_payload)
    except req.HTTPError as e:
        return jsonify({"error": f"Sell failed ({e.response.status_code}): {e.response.text[:200]}"}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    with _lock:
        if ticker in tracked:
            tracked[ticker]["status"]  = "sold"
            tracked[ticker]["sold_by"] = "human"  # manually sold via UI
    _save_tracked()

    order = result.get("order", result)
    return jsonify({"ok": True, "order_id": order.get("order_id")})


@app.route("/api/positions")
def positions():
    # Refresh current prices for open positions
    with _lock:
        snap = {k: dict(v) for k, v in tracked.items()}

    for ticker, pos in snap.items():
        if pos["status"] != "open":
            continue
        try:
            m     = _get_market(ticker)
            bid_d = m.get("yes_bid_dollars") if pos["side"] == "yes" else m.get("no_bid_dollars")
            bid   = _dollars_to_cents(bid_d)
            if bid is not None:
                pct = (bid - pos["buy_price"]) / pos["buy_price"] * 100
                with _lock:
                    if ticker in tracked:
                        tracked[ticker]["current_price"] = bid
                        tracked[ticker]["profit_pct"]    = round(pct, 1)
                snap[ticker]["current_price"] = bid
                snap[ticker]["profit_pct"]    = round(pct, 1)
        except Exception:
            pass

    return jsonify([{"ticker": k, **v} for k, v in snap.items()])


@app.route("/api/pnl")
def pnl_history():
    """Return past portfolio values at each time-window boundary for client-side PnL calc."""
    now = time.time()
    periods = [
        ("1h",  3_600),
        ("4h",  14_400),
        ("6h",  21_600),
        ("12h", 43_200),
        ("24h", 86_400),
        ("7d",  7 * 86_400),
        ("30d", 30 * 86_400),
    ]
    with _snap_lock:
        snap_copy = list(snapshots)

    past_values = {}
    for label, secs in periods:
        cutoff = now - secs
        # Earliest snapshot within this window = value at the start of the window
        entry = next((s for s in snap_copy if s["ts"] >= cutoff), None)
        past_values[label] = entry["v"] if entry else None

    return jsonify({
        "past_values":     past_values,
        "snapshots_count": len(snap_copy),
        "latest_v":        snap_copy[-1]["v"] if snap_copy else None,
    })


@app.route("/api/strategy", methods=["POST"])
def set_strategy():
    data = request.get_json(silent=True) or {}
    mode = data.get("mode", "resolution")
    pct  = float(data.get("target_pct", 10)) if data.get("target_pct") is not None else None
    dol  = float(data.get("target_dollars")) if data.get("target_dollars") is not None else None

    if mode not in ("resolution", "profit"):
        return jsonify({"error": "mode must be resolution or profit"}), 400

    tp  = float(data.get("target_price_cents"))   if data.get("target_price_cents")   is not None else None
    bip = float(data.get("buy_in_price_cents"))   if data.get("buy_in_price_cents")   is not None else None
    sell_strategy["mode"]                = mode
    if pct is not None: sell_strategy["target_pct"]         = pct
    if dol is not None: sell_strategy["target_dollars"]      = dol
    if tp  is not None: sell_strategy["target_price_cents"]  = tp
    if bip is not None: sell_strategy["buy_in_price_cents"]  = bip
    try: STRATEGY_FILE.write_text(json.dumps(sell_strategy), encoding="utf-8")
    except Exception: pass
    return jsonify({"ok": True, "strategy": sell_strategy})


@app.route("/api/sell-settings", methods=["POST"])
def set_sell_settings():
    global sell_settings
    data = request.get_json(silent=True) or {}
    if "skip_auto_sell_near_resolution" in data:
        sell_settings["skip_auto_sell_near_resolution"] = bool(data["skip_auto_sell_near_resolution"])
    if "skip_auto_sell_minutes" in data:
        sell_settings["skip_auto_sell_minutes"] = max(1, int(data["skip_auto_sell_minutes"]))
    return jsonify({"ok": True, "settings": sell_settings})


@app.route("/api/stats")
def stats():
    """Per-category win/loss/PnL stats using real Kalshi settlement data."""
    from collections import defaultdict
    with _lock:
        snap = {k: dict(v) for k, v in tracked.items()}

    # Pull settlement PnL from Kalshi (last 30 days) — same approach as /api/coach
    settle_pnl = {}
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        cursor = None
        pages  = 0
        while pages < 20:
            params = {"limit": 100}
            if cursor: params["cursor"] = cursor
            data  = kalshi_get("/portfolio/settlements", params)
            batch = data.get("settlements", [])
            if not batch: break
            stop = False
            for s in batch:
                ts_str = s.get("settled_time", "")
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if ts < cutoff: stop = True; break
                yes_cnt  = float(s.get("yes_count_fp") or 0)
                no_cnt   = float(s.get("no_count_fp")  or 0)
                yes_cost = float(s.get("yes_total_cost_dollars") or 0)
                no_cost  = float(s.get("no_total_cost_dollars")  or 0)
                rev      = float(s.get("revenue")  or 0) / 100
                cost     = yes_cost if yes_cnt > 0.001 else no_cost
                cnt      = yes_cnt if yes_cnt > 0.001 else no_cnt
                fee      = round(cnt * 0.01, 4) if rev > 0.001 else 0
                settle_pnl[s.get("ticker", "")] = rev - cost - fee
            if stop: break
            cursor = data.get("cursor")
            if not cursor: break
            pages += 1
    except Exception as e:
        print(f"[stats] settlements error: {e}")

    cats = defaultdict(lambda: {"buys": 0, "open": 0, "sold": 0,
                                "wins": 0, "losses": 0, "total_pnl": 0.0})
    for ticker, pos in snap.items():
        cat    = pos.get("category") or "Unknown"
        status = pos.get("status", "open")
        cats[cat]["buys"] += 1
        if status == "open":
            cats[cat]["open"] += 1
        elif status == "sold":
            cats[cat]["sold"] += 1
            # Prefer real settlement PnL, fall back to stored sell_price
            pnl = settle_pnl.get(ticker)
            if pnl is None:
                bp    = pos.get("buy_price",  0) or 0
                sp    = pos.get("sell_price", 0) or 0
                count = pos.get("count",      1) or 1
                if sp and sp > 0:
                    # Sold early: pnl = (sell - buy) * count / 100, minus fee only if profit
                    pnl = count * (sp - bp) / 100
                    if pnl > 0: pnl -= count * 0.01  # fee only on winning resolution
                else:
                    pp  = pos.get("profit_pct", 0) or 0
                    cost = count * bp / 100
                    if pp <= -99:
                        pnl = -cost  # total loss = just what was spent, no fee
                    else:
                        pnl = cost * (pp / 100)
                        if pnl > 0: pnl -= count * 0.01
            cats[cat]["total_pnl"] += pnl
            if pnl > 0.001: cats[cat]["wins"]   += 1
            else:           cats[cat]["losses"] += 1

    rows = []
    for cat, d in sorted(cats.items()):
        sold = d["sold"]
        rows.append({
            "category":  cat,
            "buys":      d["buys"],
            "open":      d["open"],
            "sold":      sold,
            "wins":      d["wins"],
            "losses":    d["losses"],
            "win_rate":  round(d["wins"] / sold * 100, 1) if sold else None,
            "total_pnl": round(d["total_pnl"], 2),
            "avg_pnl":   round(d["total_pnl"] / sold, 2) if sold else None,
        })

    scan_log_count = 0
    try:
        if SCAN_LOG.exists():
            scan_log_count = sum(1 for _ in SCAN_LOG.open(encoding="utf-8"))
    except Exception:
        pass

    return jsonify({
        "rows":           rows,
        "total_buys":     sum(d["buys"] for d in cats.values()),
        "total_open":     sum(d["open"] for d in cats.values()),
        "total_sold":     sum(d["sold"] for d in cats.values()),
        "scan_log_count": scan_log_count,
    })


@app.route("/api/coach")
def coach():
    """Stats analyzer over tracked positions, scan log, and recent settlements.
    Returns structured insights + recommendations the frontend renders in the Coach tab."""
    from collections import defaultdict

    with _lock:
        snap = {k: dict(v) for k, v in tracked.items()}

    # ── Build settlement lookup (last 7 days) ────────────────────────────────
    settle_pnl = {}  # ticker -> pnl (dollars, from settlements)
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        cursor = None
        pages = 0
        while pages < 20:
            params = {"limit": 100}
            if cursor: params["cursor"] = cursor
            data = kalshi_get("/portfolio/settlements", params)
            batch = data.get("settlements", [])
            if not batch: break
            stop = False
            for s in batch:
                ts_str = s.get("settled_time", "")
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z","+00:00"))
                except ValueError:
                    continue
                if ts < cutoff: stop = True; break
                yes_cnt = float(s.get("yes_count_fp") or 0)
                no_cnt  = float(s.get("no_count_fp") or 0)
                if yes_cnt > 0.001 and no_cnt > 0.001:
                    continue  # hedged
                yes_cost = float(s.get("yes_total_cost_dollars") or 0)
                no_cost  = float(s.get("no_total_cost_dollars") or 0)
                rev  = float(s.get("revenue") or 0) / 100
                cost = yes_cost if yes_cnt > 0.001 else no_cost
                cnt  = yes_cnt if yes_cnt > 0.001 else no_cnt
                fee  = round(cnt * 0.01, 4) if rev > 0.001 else 0
                settle_pnl[s.get("ticker","")] = rev - cost - fee
            if stop: break
            cursor = data.get("cursor")
            if not cursor: break
            pages += 1
    except Exception as e:
        print(f"[coach] settlements error: {e}")

    # ── Bucket helpers ───────────────────────────────────────────────────────
    def price_band(p):
        if p is None: return "unknown"
        if p < 10:  return "1-10¢"
        if p < 20:  return "10-20¢"
        if p < 30:  return "20-30¢"
        if p < 40:  return "30-40¢"
        if p < 50:  return "40-50¢"
        if p < 60:  return "50-60¢"
        if p < 70:  return "60-70¢"
        if p < 80:  return "70-80¢"
        if p < 85:  return "80-85¢"
        if p < 90:  return "85-90¢"
        if p < 95:  return "90-95¢"
        return "95-100¢"

    def hour_of_day(iso):
        try:
            return datetime.fromisoformat(iso.replace("Z","+00:00")).astimezone(timezone.utc).hour
        except Exception:
            return None

    # ── Compute outcomes from tracked positions enriched with settlement PnL ─
    cats     = defaultdict(lambda: {"n":0,"wins":0,"pnl":0.0})
    bands    = defaultdict(lambda: {"n":0,"wins":0,"pnl":0.0})
    sides    = defaultdict(lambda: {"n":0,"wins":0,"pnl":0.0})
    hours    = defaultdict(lambda: {"n":0,"wins":0,"pnl":0.0})

    total_n = 0
    total_wins = 0
    total_pnl  = 0.0

    for ticker, pos in snap.items():
        if pos.get("status") not in ("open","sold"): continue
        cat   = pos.get("category") or "Unknown"
        bp    = pos.get("buy_price")
        side  = pos.get("side") or "yes"
        count = pos.get("count") or 0
        hr    = hour_of_day(pos.get("bought_at",""))

        # Prefer Kalshi settlement PnL when available, else fall back to stored sell_price
        pnl = settle_pnl.get(ticker)
        if pnl is None:
            sp = pos.get("sell_price")
            if sp is not None and bp is not None and count:
                pnl = count * (sp - bp) / 100 - count * 0.01
            else:
                continue
        won = pnl > 0.001

        total_n += 1
        total_pnl += pnl
        if won: total_wins += 1

        for bucket, key in [(cats,cat), (bands, price_band(bp)), (sides, side)]:
            bucket[key]["n"]    += 1
            bucket[key]["pnl"]  += pnl
            if won: bucket[key]["wins"] += 1
        if hr is not None:
            hours[hr]["n"]   += 1
            hours[hr]["pnl"] += pnl
            if won: hours[hr]["wins"] += 1

    def rows_from(bucket):
        out = []
        for k, d in bucket.items():
            if d["n"] < 1: continue
            wr = d["wins"] / d["n"] * 100
            out.append({
                "key":       k,
                "n":         d["n"],
                "wins":      d["wins"],
                "losses":    d["n"] - d["wins"],
                "win_rate":  round(wr, 1),
                "total_pnl": round(d["pnl"], 2),
                "avg_pnl":   round(d["pnl"] / d["n"], 3),
            })
        return sorted(out, key=lambda r: (-r["total_pnl"], -r["n"]))

    cat_rows  = rows_from(cats)
    band_rows = rows_from(bands)
    side_rows = rows_from(sides)
    hour_rows = sorted(rows_from(hours), key=lambda r: r["key"])

    # ── Recommendations ──────────────────────────────────────────────────────
    recs = []
    sample_min = 3   # only recommend when we have enough data

    profitable_cats = [r for r in cat_rows if r["n"] >= sample_min and r["total_pnl"] > 0]
    losing_cats     = [r for r in cat_rows if r["n"] >= sample_min and r["total_pnl"] < 0]
    if profitable_cats:
        top = profitable_cats[0]
        recs.append({"type":"prefer", "msg": f"Favor **{top['key']}** — {top['win_rate']}% win rate over {top['n']} trades, +${top['total_pnl']:.2f} total."})
    if losing_cats:
        worst = min(losing_cats, key=lambda r: r["total_pnl"])
        recs.append({"type":"avoid", "msg": f"Avoid **{worst['key']}** — {worst['win_rate']}% win rate over {worst['n']} trades, ${worst['total_pnl']:.2f} total."})

    profitable_bands = [r for r in band_rows if r["n"] >= sample_min and r["total_pnl"] > 0]
    if profitable_bands:
        top = profitable_bands[0]
        recs.append({"type":"prefer", "msg": f"Best price band: **{top['key']}** ({top['win_rate']}% win rate, +${top['total_pnl']:.2f})."})

    losing_bands = [r for r in band_rows if r["n"] >= sample_min and r["total_pnl"] < 0]
    if losing_bands:
        worst = min(losing_bands, key=lambda r: r["total_pnl"])
        recs.append({"type":"avoid", "msg": f"Riskiest price band: **{worst['key']}** ({worst['win_rate']}% win rate, ${worst['total_pnl']:.2f})."})

    if total_n < 5:
        recs.append({"type":"info", "msg": f"Sample size is small ({total_n} trades) — recommendations get sharper with more data."})

    # ── Generate 5 Ranked Strategies ──────────────────────────────────────────
    strategies = []

    # Strategy 1: Conservative (High win-rate focus)
    high_wr_cats = [r for r in cat_rows if r["win_rate"] >= 70 and r["n"] >= 3]
    high_wr_bands = [r for r in band_rows if r["win_rate"] >= 70 and r["n"] >= 3]
    if high_wr_cats or high_wr_bands:
        cat_str = ", ".join([r["key"] for r in high_wr_cats[:2]]) if high_wr_cats else "any"
        band_str = ", ".join([r["key"] for r in high_wr_bands[:2]]) if high_wr_bands else "any"
        est_wr = (sum(r["win_rate"] for r in high_wr_cats[:3]) / len(high_wr_cats[:3])) if high_wr_cats else 65
        strategies.append({
            "rank": 1,
            "name": "Conservative (High Win Rate)",
            "description": f"Only buy {cat_str} at {band_str}. Targets ~{est_wr:.0f}% win rate.",
            "risk": "Low",
            "expected_wr": round(est_wr, 1),
            "pros": "High confidence, slow & steady gains",
            "cons": "Fewer trading opportunities"
        })

    # Strategy 2: Best ROI (Avg PnL focus)
    roi_cats = sorted([r for r in cat_rows if r["n"] >= 3], key=lambda r: -r["avg_pnl"])[:3]
    if roi_cats:
        best_cat = roi_cats[0]
        avg_roi = sum(r["avg_pnl"] for r in roi_cats) / len(roi_cats)
        strategies.append({
            "rank": 2,
            "name": "Best ROI",
            "description": f"Focus on {best_cat['key']} and similar high-value categories.",
            "risk": "Medium",
            "expected_wr": round(best_cat["win_rate"], 1),
            "pros": f"Best avg return per trade (${avg_roi:.2f})",
            "cons": "May have fewer trades"
        })

    # Strategy 3: Volume Play (Most liquid categories)
    vol_cats = sorted([r for r in cat_rows if r["n"] >= 5], key=lambda r: -r["n"])[:3]
    if vol_cats:
        best_vol = vol_cats[0]
        avg_wr_vol = sum(r["win_rate"] for r in vol_cats) / len(vol_cats)
        strategies.append({
            "rank": 3,
            "name": "Volume Play (Liquidity)",
            "description": f"Trade {best_vol['key']} and high-volume categories for consistent opportunities.",
            "risk": "Medium",
            "expected_wr": round(avg_wr_vol, 1),
            "pros": "Frequent trading, better order fills",
            "cons": f"Lower avg profit (${best_vol['avg_pnl']:.2f}/trade)"
        })

    # Strategy 4: Sector Leader (Best category overall)
    if cat_rows:
        leader = cat_rows[0]
        strategies.append({
            "rank": 4,
            "name": "Sector Leader",
            "description": f"Focus exclusively on **{leader['key']}** — your strongest category.",
            "risk": "Low-Medium",
            "expected_wr": round(leader["win_rate"], 1),
            "pros": f"Best overall category: {leader['win_rate']}% WR, +${leader['total_pnl']:.2f} total",
            "cons": "Narrows opportunity set"
        })

    # Strategy 5: Time-based (Best trading hours)
    best_hours = sorted([r for r in hour_rows if r["n"] >= 2], key=lambda r: (-r["win_rate"], -r["total_pnl"]))[:3]
    if best_hours:
        hour_names = []
        for h in best_hours:
            h_val = int(h["key"])
            hour_names.append(f"{h_val}:00-{h_val}:59 UTC")
        avg_wr_hour = sum(r["win_rate"] for r in best_hours) / len(best_hours)
        strategies.append({
            "rank": 5,
            "name": "Time-Based Optimization",
            "description": f"Only trade during best hours: {', '.join(hour_names[:2])}.",
            "risk": "Medium",
            "expected_wr": round(avg_wr_hour, 1),
            "pros": f"Targets prime trading windows ({avg_wr_hour:.0f}% WR)",
            "cons": "Restricted schedule, fewer total trades"
        })

    return jsonify({
        "totals": {
            "trades":    total_n,
            "wins":      total_wins,
            "losses":    total_n - total_wins,
            "win_rate":  round(total_wins / total_n * 100, 1) if total_n else None,
            "total_pnl": round(total_pnl, 2),
        },
        "by_category":   cat_rows,
        "by_price_band": band_rows,
        "by_side":       side_rows,
        "by_hour_utc":   hour_rows,
        "recommendations": recs,
        "strategies": strategies,
    })


if __name__ == "__main__":
    print("Open http://localhost:5000")
    app.run(debug=False, port=5000)
