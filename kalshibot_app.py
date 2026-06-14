#!/usr/bin/env python3
"""
Kalshi Scanner — Flask backend
Run: python app.py   →   open http://localhost:5000
"""

import base64
import concurrent.futures
import json
import socket
import math
import os
import subprocess
import sys
import threading
import time
import uuid
from collections import defaultdict

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
from flask import Flask, jsonify, request, send_from_directory, make_response

app = Flask(__name__)
HERE = Path(__file__).resolve().parent
BASE_URL = "https://api.elections.kalshi.com"
API_PREFIX = "/trade-api/v2"

# Bot identity — shown in the terminal banner/title and the web UI header so you
# always know which build is running. Bump BOT_VERSION when you ship changes.
BOT_NAME = "KalshiBot"
BOT_VERSION = "1.3.0"
DEBUG_LOGGING = False  # Set to True for verbose logs, False for production — DISABLED FOR PRODUCTION

def _log(msg: str):
    """Print with a local timestamp prefix so the terminal shows WHEN each event
    happened — essential for spotting gaps (e.g. 'bot hasn't bought in 9 hours')."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

# ── Activity log ────────────────────────────────────────────────────────────
# Append-only record of every scan cycle, buy, and sell with an epoch timestamp.
# Powers the Summary tab ("activity in the past X minutes") so you can confirm at
# a glance that the bot is alive and trading — the whole point being to NEVER again
# have a silent multi-hour gap. Kept lightweight: one JSON object per line.
# Separate logs per machine to avoid conflicts when syncing via git.
_MACHINE_NAME = os.getenv("KALSHIBOT_MACHINE", "").lower().strip()
if not _MACHINE_NAME:
    hostname = socket.gethostname().lower()
    _MACHINE_NAME = "home" if "oiokum" in hostname else "laptop"
ACTIVITY_LOG = HERE / f"activity_{_MACHINE_NAME}.log"
_activity_lock = threading.Lock()
_ACTIVITY_MAX_LINES = 50000  # trim oldest when we exceed this, so the file can't grow forever

def _record_activity(kind: str, **fields):
    """Append one activity event. `kind` is 'scan' | 'buy' | 'sell'. Extra fields
    (ticker, side, count, price, profit, detail, profile) are stored as-is."""
    try:
        entry = {"ts": time.time(), "kind": kind, **fields}
        with _activity_lock:
            with open(ACTIVITY_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
    except Exception as e:
        # Never let logging break trading — just note it once.
        print(f"[activity] write failed: {e}", flush=True)

def _read_activity(since_ts: float):
    """Read and merge activity logs from both machines, deduplicate, return oldest→newest.
    Falls back to old activity_log.jsonl for backward compatibility if per-machine logs don't exist."""
    import shutil
    out = []
    seen = set()  # deduplicate by (ts_bucket, kind, ticker, side)

    # Read from both machine logs
    for machine in ["home", "laptop"]:
        log_path = HERE / f"activity_{machine}.log"
        if not log_path.exists():
            continue
        try:
            with _activity_lock:
                lines = log_path.read_text(encoding="utf-8").splitlines()

            # Auto-trim if needed
            if len(lines) > _ACTIVITY_MAX_LINES:
                try:
                    tail = lines[-_ACTIVITY_MAX_LINES:]
                    with _activity_lock:
                        backup = log_path.parent / (log_path.name + ".bak")
                        shutil.copy2(log_path, backup)
                        temp = log_path.parent / (log_path.name + ".tmp")
                        temp.write_text("\n".join(tail) + "\n", encoding="utf-8")
                        temp.replace(log_path)
                    lines = tail
                except Exception as e:
                    print(f"[activity] trim {machine} failed: {e}", flush=True)

            for ln in lines:
                if not ln.strip():
                    continue
                try:
                    e = json.loads(ln)
                    ts = e.get("ts", 0)
                    if ts < since_ts:
                        continue
                    # Deduplicate: use (rounded_ts, kind, ticker) as unique key
                    # Round to nearest second to catch duplicates within 1s window
                    ts_bucket = int(ts)
                    kind = e.get("kind", "")
                    ticker = e.get("ticker", "")
                    side = e.get("side", "")
                    key = (ts_bucket, kind, ticker, side)
                    if key not in seen:
                        seen.add(key)
                        out.append(e)
                except Exception:
                    continue
        except Exception as e:
            print(f"[activity] read {machine} failed: {e}", flush=True)

    # Fallback: if no per-machine logs found, try old activity_log.jsonl (backward compat)
    if not out:
        old_log = HERE / "activity_log.jsonl"
        if old_log.exists():
            try:
                with _activity_lock:
                    lines = old_log.read_text(encoding="utf-8").splitlines()
                for ln in lines:
                    if not ln.strip():
                        continue
                    try:
                        e = json.loads(ln)
                        ts = e.get("ts", 0)
                        if ts < since_ts:
                            continue
                        out.append(e)
                    except Exception:
                        continue
            except Exception as e:
                print(f"[activity] read old activity_log.jsonl failed: {e}", flush=True)

    # Sort by timestamp, oldest first
    out.sort(key=lambda x: x.get("ts", 0))
    return out

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

# Accept several filenames so you're never stuck with one exact name. The API key
# is a plain-text UUID; the private key is an RSA/EC PEM file. The old setup used
# the confusing name "test2.txt" for the private key — still accepted, but you can
# now use the clearer "kalshi_private_key.pem" instead.
_API_KEY_NAMES = ["kalshi_api_key", "kalshi_api_key.txt"]
_PRIV_KEY_NAMES = ["kalshi_private_key.pem", "kalshi_private_key", "kalshi_private_key.txt", "test2.txt"]

def _find_cred_file(names: list) -> Path:
    """Return the first existing file from `names` inside CREDS_DIR, else None."""
    for n in names:
        p = CREDS_DIR / n
        if p.exists():
            return p
    return None

def _load_creds():
    key_file = _find_cred_file(_API_KEY_NAMES)
    if key_file is None:
        raise FileNotFoundError(
            f"Kalshi API key file not found.\n"
            f"  Looked in folder : {CREDS_DIR}\n"
            f"  Expected one of  : {', '.join(_API_KEY_NAMES)}\n"
            f"  Fix: create that folder (next to the bot folder) and save your\n"
            f"       Kalshi API key UUID into a plain-text file named 'kalshi_api_key'."
        )
    priv_file = _find_cred_file(_PRIV_KEY_NAMES)
    if priv_file is None:
        raise FileNotFoundError(
            f"Kalshi private key file not found.\n"
            f"  Looked in folder : {CREDS_DIR}\n"
            f"  Expected one of  : {', '.join(_PRIV_KEY_NAMES)}\n"
            f"  Fix: save your Kalshi RSA private key (PEM) into that folder as\n"
            f"       'kalshi_private_key.pem'."
        )

    key = key_file.read_text(encoding="utf-8").lstrip("﻿").strip()
    pem = priv_file.read_bytes()
    pk = serialization.load_pem_private_key(pem, password=None, backend=default_backend())
    return key, pk, key_file, priv_file

try:
    API_KEY, PRIVATE_KEY, _key_file, _priv_file = _load_creds()
    key_type = "RSA" if isinstance(PRIVATE_KEY, RSAPrivateKey) else "EC" if isinstance(PRIVATE_KEY, EllipticCurvePrivateKey) else type(PRIVATE_KEY).__name__
    print(f"Credentials loaded OK. Key prefix: {API_KEY[:8]}... len={len(API_KEY)} | Private key type: {key_type}")
    print(f"Creds dir   : {CREDS_DIR}")
    print(f"API key file: {_key_file.name}  |  Private key file: {_priv_file.name}")
except Exception as e:
    print("=" * 70)
    print("ERROR loading Kalshi credentials:")
    print(e)
    print(f"\nPut your two key files in this exact folder:\n  {CREDS_DIR}")
    print("=" * 70)
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

# Rate limiting. The old code used a flat 2s/call with NO retry, which starved the
# balance/cash fetch behind the scan loop's ~30+ serialized calls (cash showed "—")
# and let a single 429 fail outright. Now: a smaller base gap between calls PLUS real
# 429 backoff+retry — faster in the common case and actually robust when throttled.
_last_api_call = {"time": 0}
_last_hipri_call = {"time": 0}   # timestamp of the last USER-facing (high-priority) call
_rate_limit_delay = 0.5   # base seconds between API calls (was 2.0)

# ── API activity log — every OUTBOUND Kalshi call, for the "API Log" tab so you can
# see how busy the bot is behind the scenes and spot rate-limiting (429s).
from collections import deque
_api_log = deque(maxlen=500)   # each: {ts, method, ep, status, ms}
def _log_api(method, ep, status, ms):
    try:
        _api_log.append({"ts": time.time(), "method": method,
                         "ep": ep, "status": status, "ms": round(ms)})
    except Exception as e:
        print(f"[api_log] record error: {e}")
_max_retries = 4
_api_lock = threading.Lock()

def _rate_limit_wait(low_priority: bool = False):
    """Space out API calls. Holds the lock only briefly — NOT during 429 backoff.

    PRIORITY: the scan/buy loop calls with low_priority=True. If a user-facing
    (high-priority) request — positions, settlements, balance, save, sell — happened
    in the last ~1.5s, low-priority callers YIELD so the user's request gets the API
    first and never gets starved behind the constant scan traffic. After the user's
    burst goes quiet, the scan loop resumes."""
    if low_priority:
        waited = 0.0
        while (time.time() - _last_hipri_call["time"]) < 1.5 and waited < 8.0:
            time.sleep(0.1)
            waited += 0.1
    with _api_lock:
        now = time.time()
        elapsed = now - _last_api_call["time"]
        if elapsed < _rate_limit_delay:
            time.sleep(_rate_limit_delay - elapsed)
        _last_api_call["time"] = time.time()
        if not low_priority:
            _last_hipri_call["time"] = time.time()

def _kalshi_request(method: str, endpoint: str, params: dict = None,
                    body: dict = None, retry_on_timeout: bool = True,
                    low_priority: bool = False) -> dict:
    path = API_PREFIX + endpoint
    last_exc = None
    for attempt in range(_max_retries):
        _rate_limit_wait(low_priority=low_priority)
        _t0 = time.time()
        try:
            if method == "GET":
                r = req.get(BASE_URL + path, headers=_headers("GET", path),
                            params=params or {}, timeout=20)
            else:
                body_str = json.dumps(body, separators=(',', ':'))
                r = req.post(BASE_URL + path, headers=_headers("POST", path),
                             data=body_str, timeout=15)
            _log_api(method, endpoint, r.status_code, (time.time() - _t0) * 1000)
            if r.status_code == 429:
                ra = r.headers.get("Retry-After")
                try: wait = float(ra) if ra else 0
                except ValueError: wait = 0
                if wait <= 0:
                    wait = min(8.0, 1.0 * (2 ** attempt))  # 1s, 2s, 4s, 8s
                print(f"[API 429] {method} {endpoint} — backoff {wait:.1f}s ({attempt+1}/{_max_retries})")
                time.sleep(wait)
                continue
            if not r.ok:
                print(f"[API {r.status_code}] {method} {endpoint} -> {r.text[:500]}")
            r.raise_for_status()
            return r.json()
        except req.HTTPError:
            raise  # non-429 HTTP error — surface it, don't retry
        except (req.Timeout, req.ConnectionError) as e:
            last_exc = e
            if not retry_on_timeout:
                raise  # POST: unknown state, don't risk a duplicate order
            wait = min(8.0, 1.0 * (2 ** attempt))
            print(f"[API timeout] {method} {endpoint} — retry in {wait:.1f}s ({attempt+1}/{_max_retries})")
            time.sleep(wait)
            continue
    if last_exc:
        raise last_exc
    raise RuntimeError(f"Kalshi {method} {endpoint}: still rate-limited after {_max_retries} tries")

# Thread id of the headless bot loop. Any Kalshi GET made from inside that thread is
# automatically LOW priority, so the constant scan traffic yields to user-facing
# requests (positions / settlements / saves) without marking every call by hand.
_bot_thread_id = None

def kalshi_get(endpoint: str, params: dict = None, low_priority: bool = None) -> dict:
    if low_priority is None:
        low_priority = (_bot_thread_id is not None and threading.get_ident() == _bot_thread_id)
    return _kalshi_request("GET", endpoint, params=params, low_priority=low_priority)

def kalshi_post(endpoint: str, body: dict) -> dict:
    # POST = order placement. Retry 429 (rejected before processing, and the
    # client_order_id makes a resend idempotent) but NOT timeouts — a timed-out
    # order may have actually executed, so a blind retry could double-fill.
    return _kalshi_request("POST", endpoint, body=body, retry_on_timeout=False)

# ---------------------------------------------------------------------------
# Position tracking & sell strategy
# ---------------------------------------------------------------------------

_lock = threading.RLock()  # Reentrant lock to allow nested acquisitions (e.g., _save_tracked())

TRACKED_FILE   = HERE / "bot_positions.json"
STRATEGY_FILE  = HERE / "bot_strategy.json"
SAVED_STRATS_FILE = HERE / "bot_saved_strategies.json"
BUY_SETTINGS_FILE = HERE / "buy_settings.json"  # the auto-bot's BUY filters, set from the UI
SCAN_LOG       = HERE / "scan_log.jsonl"      # append-only; one JSON line per scan run

def _save_tracked():
    """Save tracked positions to disk. Must be called with _lock held, or acquires lock internally."""
    # If called from within a lock, this is safe. If called outside, we acquire the lock.
    # This is a bit of defensive coding — ideally all calls should be inside locks already.
    try:
        with _lock:
            TRACKED_FILE.write_text(json.dumps(tracked, default=str), encoding="utf-8")
    except Exception as e:
        print(f"[tracked] save error: {e}")

# { ticker: { side, count, buy_price, title, strategy, target_pct, bought_at, status } }
tracked: dict = {}

# Queue of recent bot-made buys that the frontend hasn't announced yet
_recent_buys_queue = []  # [{"ticker": "...", "side": "...", "count": ..., "spent": ..., "category": "..."}, ...]
try:
    if TRACKED_FILE.exists():
        tracked = json.loads(TRACKED_FILE.read_text(encoding="utf-8"))
        print(f"Loaded {len(tracked)} tracked positions from {TRACKED_FILE.name}")
except Exception as e:
    print(f"[tracked] load error: {e}")

# Recently-sold registry: { ticker: {"side": "yes"|"no", "at": epoch_seconds} }
# Populated by /api/sell for ANY sold position (tracked or not). The portfolio
# endpoint hides these for RECENTLY_SOLD_TTL seconds so a position you just sold
# doesn't reappear from Kalshi's brief settlement-propagation delay — even across
# a hard page refresh (which wipes the frontend's in-memory list). Self-expires,
# so a position genuinely still held (e.g. an unfilled resting order) returns
# after the window instead of being hidden forever.
_recently_sold: dict = {}
RECENTLY_SOLD_TTL = 120  # seconds

def _is_recently_sold(ticker: str, side: str) -> bool:
    """True if `ticker`/`side` was sold within the last RECENTLY_SOLD_TTL seconds.
    Prunes expired entries as a side effect so the dict stays small."""
    with _lock:
        entry = _recently_sold.get(ticker)
        if not entry:
            return False
        if time.time() - entry.get("at", 0) > RECENTLY_SOLD_TTL:
            _recently_sold.pop(ticker, None)  # expired — clean up
            return False
        return entry.get("side") == side

# Sell strategy settings (updated from frontend)
sell_settings = {
    "skip_auto_sell_near_resolution": True,
    "skip_auto_sell_minutes": 1,
}

# Global sell strategy — load from file so it survives Flask restarts
try:
    sell_strategy = json.loads(STRATEGY_FILE.read_text(encoding="utf-8")) if STRATEGY_FILE.exists() else {}
except Exception as e:
    print(f"[strategy] load error: {e}")
    sell_strategy = {}
sell_strategy.setdefault("mode", "resolution")
sell_strategy.setdefault("target_pct", 10.0)
print(f"[strategy] loaded: mode={sell_strategy.get('mode')} target_pct={sell_strategy.get('target_pct')} target_dollars={sell_strategy.get('target_dollars')}")

# ── BUY settings ───────────────────────────────────────────────────────────
# The auto-bot's BUY filters. Set from the UI (POST /api/buy-settings), read LIVE
# by _bot_thread every cycle — this is the bridge that makes the headless bot trade
# what the UI says instead of hardcoded 80–96% crypto YES.
_DEFAULT_BUY_SETTINGS = {
    "enable_buy_up":   True,  "up_min":   80.0, "up_max":   96.0,  # YES side range
    "enable_buy_down": False, "down_min": 80.0, "down_max": 96.0,  # NO side range
    "minutes":         15,        # only markets closing within this many minutes
    "buy_amount":      1.00,      # MAX dollars to spend per buy (matches the UI default)
    "max_per_scan":    3,         # max NEW buys per 15s cycle (rate-limit safety)
    "max_concurrent":  999,       # max total open positions
    "max_per_market":  1,         # max buys per ticker
    "show_crypto": True, "show_combo": False, "show_sports": False,
    "show_politics": False, "show_economics": False,
    "good_liq": True, "hide_multi": True,
    "min_age_mins": None, "max_age_mins": None, "no_buy_within_mins": None,
}
try:
    _bs_loaded = json.loads(BUY_SETTINGS_FILE.read_text(encoding="utf-8")) if BUY_SETTINGS_FILE.exists() else {}
except Exception as e:
    print(f"[buy_settings] load error: {e}")
    _bs_loaded = {}
buy_settings = {**_DEFAULT_BUY_SETTINGS, **_bs_loaded}

# ── Strategy profiles (multi-bot tabs) ─────────────────────────────────────
# Each UI tab is its own bot "profile" with a full settings set. Only ONE profile
# is ACTIVE at a time (one buy at a time), and the live `buy_settings` the bot reads
# is always a mirror of the active profile. T1 = the front-page (Scanner) bot, T2 =
# the Lotto tab bot. Backward compatible: if no profile is given anywhere, everything
# operates on the active profile, exactly like before this feature existed.
PROFILES_FILE = HERE / "profiles.json"
PROFILE_IDS = ["T1", "T2"]

def _load_profiles():
    try:
        if PROFILES_FILE.exists():
            d = json.loads(PROFILES_FILE.read_text(encoding="utf-8"))
            # Tolerate a legacy/corrupt file that's just a bare list of active
            # profile IDs (e.g. ["T1","T2"]) — treat it as active_profiles.
            if isinstance(d, list):
                act = [p for p in d if p in PROFILE_IDS] or ["T1"]
                return {}, {}, act
            if not isinstance(d, dict):
                return {}, {}, ["T1"]
            prof = d.get("profiles") or {}
            sell = d.get("sell") or {}
            # Multiple profiles can be active at once. Accept the new "active_profiles"
            # list, or migrate the legacy single "active" string.
            act = d.get("active_profiles")
            if not isinstance(act, list):
                legacy = d.get("active")
                act = [legacy] if legacy in PROFILE_IDS else ["T1"]
            act = [p for p in act if p in PROFILE_IDS] or ["T1"]
            return prof, sell, act
    except Exception as e:
        print(f"[profiles] load failed: {e}")
    return {}, {}, ["T1"]

_DEFAULT_SELL_STRATEGY = {"mode": "resolution", "target_pct": 10.0}

# Lotto (T2) starting defaults — set per the user's spec: cheap long-shots priced
# 1¢–15¢, one buy per market, markets ending within 365 days, hold to resolution
# with a 50% stop-loss. The user tunes these on the Lotto page (and can store sets
# in the M1–M5 memory slots there).
_LOTTO_BUY_DEFAULTS = {
    "enable_buy_up":   True,  "up_min":   1.0,  "up_max":   15.0,
    "enable_buy_down": False, "down_min": 1.0,  "down_max": 15.0,
    "minutes":         525600,    # 365 days
    "buy_amount":      0.25,       # max dollars per buy (matches the UI default)
    "max_per_scan":    10,
    "max_concurrent":  999,
    "max_per_market":  1,          # max 1 buy per contract
    "show_crypto": True, "show_combo": True, "show_sports": True,
    "show_politics": True, "show_economics": True,
    "good_liq": False, "hide_multi": False,
    "min_age_mins": None, "max_age_mins": None, "no_buy_within_mins": None,
}
_LOTTO_SELL_DEFAULTS = {"mode": "resolution", "target_pct": 10.0, "stop_loss_pct": 50.0}

_profiles, _sell_profiles, active_profiles = _load_profiles()
# Seed buy profiles. T1 inherits the existing live buy_settings (so the current bot
# is unchanged); T2 (Lotto) seeds with the cheap long-shot preset above.
_profiles.setdefault("T1", dict(buy_settings))
_profiles.setdefault("T2", dict(_LOTTO_BUY_DEFAULTS))
for _pid in PROFILE_IDS:
    _profiles[_pid] = {**_DEFAULT_BUY_SETTINGS, **_profiles[_pid]}
# Seed sell profiles. T1 inherits the existing global sell_strategy; T2 = lotto preset.
_sell_profiles.setdefault("T1", dict(sell_strategy))
_sell_profiles.setdefault("T2", dict(_LOTTO_SELL_DEFAULTS))
for _pid in PROFILE_IDS:
    _sell_profiles[_pid] = {**_DEFAULT_SELL_STRATEGY, **_sell_profiles[_pid]}
# The bot reads EACH active profile directly from _profiles/_sell_profiles. The globals
# below are a legacy mirror of T1 (the front-page bot) used as a fallback by the sell
# monitor and the endpoints' no-profile default.
buy_settings  = dict(_profiles["T1"])
sell_strategy = dict(_sell_profiles["T1"])

def _save_profiles():
    try:
        PROFILES_FILE.write_text(json.dumps(
            {"active_profiles": active_profiles, "profiles": _profiles, "sell": _sell_profiles}, indent=2),
            encoding="utf-8")
    except Exception as e:
        print(f"[profiles] save error: {e}")

def _save_buy_settings():
    """Persist the T1 legacy mirror + the profiles file."""
    global _profiles
    try:
        BUY_SETTINGS_FILE.write_text(json.dumps(buy_settings, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[buy settings] save error: {e}")
    _profiles["T1"] = dict(buy_settings)
    _save_profiles()

def _apply_buy_edits(target: dict, data: dict):
    """Coerce + apply a posted settings payload onto `target` (a settings dict).
    Shared by the buy-settings endpoint for both active and inactive profiles."""
    def _pct(v, default):
        try:    return max(1.0, min(99.0, float(v)))
        except (TypeError, ValueError): return default
    def _posint(v, default, lo=0):
        try:    return max(lo, int(float(v)))
        except (TypeError, ValueError): return default
    def _posfloat(v, default, lo=0.0):
        # Like _posint but keeps fractional values (the "ends within" custom box
        # allows 0.25–5 min, e.g. 0.5 = last 30 seconds). Round to 2dp to avoid
        # float noise. Drop the trailing .0 so whole minutes stay clean ints.
        try:
            f = max(lo, round(float(v), 2))
            return int(f) if f == int(f) else f
        except (TypeError, ValueError): return default
    def _optfloat(v):
        if v in (None, "", "any", "off"): return None
        try:    return float(v)
        except (TypeError, ValueError): return None

    for k in ["enable_buy_up", "enable_buy_down", "show_crypto", "show_combo",
              "show_sports", "show_politics", "show_economics", "good_liq", "hide_multi"]:
        if k in data:
            target[k] = bool(data[k])
    for k in ("up_min", "up_max", "down_min", "down_max"):
        if k in data:
            target[k] = _pct(data[k], target.get(k, 80.0))
    if "minutes" in data:
        target["minutes"] = _posfloat(data["minutes"], target.get("minutes", 15), lo=0.25)
    if "buy_amount" in data:
        try:    target["buy_amount"] = max(0.01, float(data["buy_amount"]))
        except (TypeError, ValueError): pass
    if "max_per_scan" in data:
        target["max_per_scan"] = _posint(data["max_per_scan"], target.get("max_per_scan", 3), lo=1)
    if "max_concurrent" in data:
        target["max_concurrent"] = _posint(data["max_concurrent"], target.get("max_concurrent", 999), lo=1)
    if "max_per_market" in data:
        target["max_per_market"] = _posint(data["max_per_market"], target.get("max_per_market", 1), lo=1)
    for k in ("min_age_mins", "max_age_mins", "no_buy_within_mins"):
        if k in data:
            target[k] = _optfloat(data[k])
    # Keep min <= max for each side
    if target.get("up_min", 0) > target.get("up_max", 0):
        target["up_min"], target["up_max"] = target["up_max"], target["up_min"]
    if target.get("down_min", 0) > target.get("down_max", 0):
        target["down_min"], target["down_max"] = target["down_max"], target["down_min"]

print(f"[buy] loaded: up={buy_settings['enable_buy_up']}({buy_settings['up_min']}-{buy_settings['up_max']}) "
      f"down={buy_settings['enable_buy_down']}({buy_settings['down_min']}-{buy_settings['down_max']}) "
      f"amt=${buy_settings['buy_amount']} win={buy_settings['minutes']}m")

# In-memory cache of event_ticker → clean title
_event_cache: dict = {}

# Persistent ticker → pretty (human-readable) title cache. Titles never change, so
# once we've resolved one (via enrichment), remember it on disk and reuse it on
# EVERY load — including the fast path — so positions always show the readable name
# instead of the raw ticker, with zero waiting for enrichment.
_title_cache: dict = {}
TITLE_CACHE_FILE = HERE / "title_cache.json"

def _load_title_cache():
    global _title_cache
    try:
        if TITLE_CACHE_FILE.exists():
            d = json.loads(TITLE_CACHE_FILE.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                _title_cache = d
    except Exception as e:
        print(f"[title-cache] load failed: {e}")

def _save_title_cache():
    try:
        TITLE_CACHE_FILE.write_text(json.dumps(_title_cache), encoding="utf-8")
    except Exception as e:
        print(f"[title-cache] save failed: {e}")

def _remember_title(ticker: str, title: str):
    """Store a resolved pretty title (only if it's actually pretty, not the ticker)."""
    if ticker and title and title != ticker and _title_cache.get(ticker) != title:
        _title_cache[ticker] = title
        _save_title_cache()

_COIN_NAMES = {
    "BTC": "Bitcoin", "BTCD": "Bitcoin", "BTCW": "Bitcoin", "BTCM": "Bitcoin",
    "ETH": "Ethereum", "ETHD": "Ethereum", "ETHUSD": "Ethereum",
    "SOL": "Solana", "SOLD": "Solana",
    "XRP": "XRP", "DOGE": "Dogecoin", "ADA": "Cardano", "AVAX": "Avalanche",
    "LINK": "Chainlink", "BNB": "BNB", "LTC": "Litecoin", "MATIC": "Polygon",
    "DOT": "Polkadot", "SHIB": "Shiba Inu", "PEPE": "Pepe", "TRX": "Tron",
    "ATOM": "Cosmos", "HYPE": "Hyperliquid",
}

def _humanize_ticker(ticker: str) -> str:
    """Last-resort readable label when no real title is available — so the UI shows
    something human instead of a raw code like 'KXBTCD-26JUN0717-T63249.99'.
    Recognizes crypto price markets (KX<COIN>-<date>-<B/T><strike>); otherwise
    returns the ticker unchanged (no worse than before)."""
    if not ticker:
        return ticker
    parts = ticker.upper().split("-")
    head = parts[0]
    if head.startswith("KX") and len(parts) >= 3:
        coin = head[2:]                       # KXBTCD -> BTCD
        name = _COIN_NAMES.get(coin, coin)    # BTCD -> "Bitcoin"
        # Parse strike from last part; strip leading B/T prefix (e.g. "T63249.99" -> 63249.99)
        raw_strike = parts[-1].lstrip("BTbt")
        try:
            val = float(raw_strike)
            if val == int(val):
                return f"{name} ${int(val):,}"
            else:
                return f"{name} ${val:,.2f}"
        except ValueError:
            pass
        return name
    return ticker

def _pretty_title(ticker: str, resolved: str) -> str:
    """Best available title: a freshly-resolved pretty one, else the disk cache,
    else a humanized version of the ticker (never the raw code if we can help it).
    Also remembers a freshly-resolved pretty title for next time."""
    if resolved and resolved != ticker:
        _remember_title(ticker, resolved)
        return resolved
    return _title_cache.get(ticker) or _humanize_ticker(ticker)

_load_title_cache()

# Settlements cache — shared between stats, coach, portfolio (avoids 3x duplicate queries)
_settlements_cache: dict = {}  # hours → {"data": [...], "ts": float}
_SETTLEMENTS_CACHE_TTL = 120.0  # 2 minutes

# Self-diagnostic record — surfaced in /api/summary so we never debug settlements blind.
# Populated on every _recent_settlements run (incl. the background refresh thread).
_settle_debug: dict = {
    "raw": 0,             # rows Kalshi /portfolio/settlements returned (all pages)
    "in_window": 0,       # rows inside the requested time cutoff
    "skipped_empty": 0,   # dropped: no market_result AND no counts AND no revenue
    "skipped_hedged": 0,  # dropped: held both yes+no (net-zero hedge)
    "kept": 0,            # rows that became settlement entries
    "last_error": "",     # exception text from the last run, if any
    "sample_keys": [],    # field names of the first raw row (to verify API shape)
    "ran_at": 0.0,        # epoch of last run
}

# Persist the settlements cache to disk so a restart/Update doesn't wipe it. The
# fetch is non-blocking and the bot's heavy API traffic can starve the background
# refresh for minutes — without this, the Summary tab shows ONLY buys (no settled
# rows) for a long time after every restart. Loading the disk copy on startup makes
# settled rows appear instantly while the fresh fetch warms up behind the scenes.
SETTLEMENTS_CACHE_FILE = HERE / "settlements_cache.json"

def _save_settlements_cache():
    try:
        # Keys are ints (hours) → JSON-stringify them; restore on load.
        blob = {str(k): v for k, v in _settlements_cache.items()}
        SETTLEMENTS_CACHE_FILE.write_text(json.dumps(blob, default=str), encoding="utf-8")
    except Exception as e:
        print(f"[settlements-cache] save failed: {e}")

def _load_settlements_cache():
    global _settlements_cache
    try:
        if SETTLEMENTS_CACHE_FILE.exists():
            d = json.loads(SETTLEMENTS_CACHE_FILE.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                restored = {}
                for k, v in d.items():
                    try:
                        restored[int(k)] = v
                    except (TypeError, ValueError):
                        continue
                if restored:
                    _settlements_cache = restored
                    total = sum(len(v.get("data", [])) for v in restored.values())
                    print(f"[settlements-cache] loaded {total} settlements from disk")
    except Exception as e:
        print(f"[settlements-cache] load failed: {e}")

def _cached_settlements(hours: int = 24) -> list:
    """Return settlements from cache if fresh, otherwise fetch and cache."""
    now = time.time()
    cached = _settlements_cache.get(hours)
    if cached and (now - cached["ts"]) < _SETTLEMENTS_CACHE_TTL:
        return cached["data"]
    data = _recent_settlements(hours=hours)
    _settlements_cache[hours] = {"data": data, "ts": now}
    _save_settlements_cache()
    return data

_settle_refresh_inflight: set = set()
def _cached_settlements_nonblocking(hours: int = 24) -> list:
    """NEVER blocks: returns whatever is cached (even stale, even empty) and
    refreshes in a background thread. The settlements fetch can take 30-60s
    through the rate limiter — that wait was leaving the Summary tab blank."""
    now = time.time()
    cached = _settlements_cache.get(hours)
    fresh = cached and (now - cached["ts"]) < _SETTLEMENTS_CACHE_TTL
    if not fresh and hours not in _settle_refresh_inflight:
        _settle_refresh_inflight.add(hours)
        def _refresh():
            try:
                data = _recent_settlements(hours=hours)
                _settlements_cache[hours] = {"data": data, "ts": time.time()}
                _save_settlements_cache()   # survive the next restart
            except Exception as e:
                print(f"[settlements] background refresh failed: {e}")
            finally:
                _settle_refresh_inflight.discard(hours)
        threading.Thread(target=_refresh, daemon=True).start()
    return cached["data"] if cached else []

# Warm the settlements cache from disk immediately at import so the very first
# Summary request after a restart already has settled rows to show.
_load_settlements_cache()

# ── Order fills — Kalshi's authoritative trade history ──────────────────────
# /portfolio/fills is the record of EVERY buy and sell on the account, kept by
# Kalshi — complete no matter which machine made the trade or whether the bot
# was running. This is what lets the Summary show real history from any device.
_fills_cache: dict = {"data": [], "ts": 0.0}
_FILLS_CACHE_TTL = 120.0
_fills_refresh_inflight = threading.Event()

def _settle_counts(s: dict):
    """Extract (yes_cnt, no_cnt, yes_cost_dollars, no_cost_dollars) from a Kalshi
    settlement object, trying multiple known field-name variants across API versions."""
    def _fv(*keys):
        for k in keys:
            v = s.get(k)
            try:
                fv = float(v)
                if fv > 0:
                    return fv
            except (TypeError, ValueError):
                continue
        return 0.0
    yes_cnt = _fv("yes_count_fp", "yes_count", "yes_contracts", "yes_position")
    no_cnt  = _fv("no_count_fp",  "no_count",  "no_contracts",  "no_position")

    # If counts still zero, try generic count field + market_result to assign side
    if yes_cnt < 0.001 and no_cnt < 0.001:
        generic = _fv("count", "contracts", "quantity", "position", "number_of_contracts")
        if generic > 0.001:
            result = str(s.get("market_result", "")).lower()
            if result == "yes":
                yes_cnt = generic
            else:
                no_cnt = generic

    def _cost_dollars(cost_raw, cnt):
        try:
            v = float(cost_raw or 0)
            # If per-contract value > $1.50 when expressed as-is, it's in cents
            if v > 1 and cnt > 0.001 and (v / cnt) > 1.5:
                v /= 100
            return round(v, 4)
        except (TypeError, ValueError):
            return 0.0

    yc = _cost_dollars(s.get("yes_total_cost_dollars") or s.get("yes_total_cost"), yes_cnt)
    nc = _cost_dollars(s.get("no_total_cost_dollars")  or s.get("no_total_cost"),  no_cnt)
    return yes_cnt, no_cnt, yc, nc


def _recent_fills(hours: int = 24 * 30) -> list:
    """Fetch all fills in the window, paginated (200/page, capped at 25 pages)."""
    out = []
    min_ts = int(time.time() - hours * 3600)
    cursor = None
    for _ in range(25):
        params = {"limit": 200, "min_ts": min_ts}
        if cursor:
            params["cursor"] = cursor
        d = kalshi_get("/portfolio/fills", params)
        fills = d.get("fills", [])
        out.extend(fills)
        cursor = d.get("cursor")
        if not cursor or not fills:
            break
    # Dedup: Kalshi (or cursor-page overlap at the min_ts boundary) can return the
    # SAME fill more than once, which showed up as phantom duplicate BUY rows in the
    # Summary (e.g. one 3-contract ETH buy appearing 3×). trade_id is the unique fill
    # id — real distinct fills keep their own id, so this only removes true repeats.
    seen, deduped = set(), []
    for f in out:
        key = f.get("trade_id") or f.get("fill_id") or (
            f.get("ticker"), f.get("created_time"), f.get("side"),
            f.get("action"), f.get("count"), f.get("yes_price"), f.get("no_price"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(f)
    return deduped

def _cached_fills_nonblocking() -> list:
    """NEVER blocks: serve the cached fills and refresh in a background thread
    (same pattern as settlements — a blocking fetch would hang the Summary)."""
    now = time.time()
    fresh = (now - _fills_cache["ts"]) < _FILLS_CACHE_TTL
    if not fresh and not _fills_refresh_inflight.is_set():
        _fills_refresh_inflight.set()
        def _refresh():
            try:
                data = _recent_fills(hours=24 * 30)
                _fills_cache["data"] = data
                _fills_cache["ts"] = time.time()
            except Exception as e:
                print(f"[fills] background refresh failed: {e}")
            finally:
                _fills_refresh_inflight.clear()
        threading.Thread(target=_refresh, daemon=True).start()
    return _fills_cache["data"]

# ── Kalshi trading fee (real formula, replaces the old flat 1¢ guess) ────────
# Kalshi taker fee = ceil_to_cent(0.07 × contracts × P × (1−P)) where P = price
# in dollars. Source: kalshi.com/fee-schedule. A 50¢ contract costs 1.75¢/contract;
# a 3¢ lotto contract costs ~0.2¢ → rounds up to 1¢; an 80¢ contract costs 1.12¢ → 2¢.
def _kalshi_fee(count: float, price_cents: float) -> float:
    try:
        p = max(0.0, min(1.0, float(price_cents) / 100.0))
        raw = 0.07 * float(count) * p * (1.0 - p)
        return math.ceil(raw * 100) / 100.0  # Kalshi rounds UP to the next cent
    except Exception:
        return 0.0

# ── Spread Guard — cushion-aware buying for 15-min crypto markets ────────────
# Settlement fact (verified): Kalshi crypto markets settle on CF Benchmarks
# Real-Time Indexes (BRTI for BTC) — the final value is the AVERAGE of 60
# once-per-second RTI prices over the LAST MINUTE before close. So a market
# whose spot sits $40 from the strike with 2 min left genuinely can flip; one
# sitting $400 away almost never does. The guard measures that cushion live.
# Live spot prices — Coinbase first, Kraken fallback. These are constituent
# exchanges of the CF Benchmarks RTI that Kalshi settles on, so they're an
# honest proxy for the settlement index. 10s cache keeps API traffic tiny.
_spot_cache: dict = {}  # symbol → {"price": float, "ts": float, "source": str}
_SPOT_CACHE_TTL = 10.0
_KRAKEN_PAIRS = {"BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD",
                 "DOGE": "DOGEUSD", "XRP": "XRPUSD", "BNB": None}

def _spot_price(symbol: str):
    """Return (price, source) for a crypto symbol, or (None, reason)."""
    now = time.time()
    c = _spot_cache.get(symbol)
    if c and (now - c["ts"]) < _SPOT_CACHE_TTL:
        return c["price"], c["source"]
    # 1) Coinbase spot (RTI constituent exchange)
    try:
        r = req.get(f"https://api.coinbase.com/v2/prices/{symbol}-USD/spot", timeout=4)
        if r.ok:
            px = float(r.json()["data"]["amount"])
            _spot_cache[symbol] = {"price": px, "ts": now, "source": "coinbase"}
            return px, "coinbase"
    except Exception:
        pass
    # 2) Kraken (also an RTI constituent)
    pair = _KRAKEN_PAIRS.get(symbol)
    if pair:
        try:
            r = req.get(f"https://api.kraken.com/0/public/Ticker?pair={pair}", timeout=4)
            if r.ok:
                res = r.json().get("result", {})
                first = next(iter(res.values()), None)
                if first:
                    px = float(first["c"][0])
                    _spot_cache[symbol] = {"price": px, "ts": now, "source": "kraken"}
                    return px, "kraken"
        except Exception:
            pass
    return None, "no_source"

# ── Smart Strategy — master switch for auto Scanner/Lotto based on spread ────
SMART_STRATEGY_FILE = HERE / "smart_strategy.json"
smart_strategy = {
    "enabled": False,
    "mode": None,  # "scanner", "lotto", or None (auto-selecting)
    "spread_threshold": 100.0,  # $100 or more = use Scanner, less = use Lotto
}
def _load_smart_strategy():
    global smart_strategy
    try:
        if SMART_STRATEGY_FILE.exists():
            d = json.loads(SMART_STRATEGY_FILE.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                smart_strategy.update(d)
    except Exception as e:
        print(f"[smart-strategy] load failed: {e}")
def _save_smart_strategy():
    try:
        SMART_STRATEGY_FILE.write_text(json.dumps(smart_strategy, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[smart-strategy] save failed: {e}")
_load_smart_strategy()

# ── Per-token Smart Strategy settings (from localStorage / frontend config) ──
# Each token tracks: enabled, buy_last_min, threshold, mode, order_type, ai_choice
PER_TOKEN_SETTINGS_FILE = HERE / "per_token_settings.json"
per_token_settings = {
    "BTC": {"enabled": False, "buy_last_min": 1, "threshold": 100, "mode": "both", "order_type": "market", "ai_choice": True},
    "ETH": {"enabled": False, "buy_last_min": 1, "threshold": 100, "mode": "both", "order_type": "market", "ai_choice": True},
    "SOL": {"enabled": False, "buy_last_min": 1, "threshold": 100, "mode": "both", "order_type": "market", "ai_choice": True},
    "XRP": {"enabled": False, "buy_last_min": 1, "threshold": 0.25, "mode": "both", "order_type": "market", "ai_choice": True},
    "DOGE": {"enabled": False, "buy_last_min": 1, "threshold": 100, "mode": "both", "order_type": "market", "ai_choice": True},
    "BNB": {"enabled": False, "buy_last_min": 1, "threshold": 100, "mode": "both", "order_type": "market", "ai_choice": True},
    "HYPE": {"enabled": False, "buy_last_min": 1, "threshold": 100, "mode": "both", "order_type": "market", "ai_choice": True},
}

def _load_per_token_settings():
    global per_token_settings
    try:
        if PER_TOKEN_SETTINGS_FILE.exists():
            d = json.loads(PER_TOKEN_SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                for token in per_token_settings:
                    if token in d:
                        per_token_settings[token].update(d[token])
    except Exception as e:
        print(f"[per-token] settings load failed: {e}")

def _save_per_token_settings():
    try:
        PER_TOKEN_SETTINGS_FILE.write_text(json.dumps(per_token_settings, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[per-token] settings save failed: {e}")

_load_per_token_settings()

# ── Per-token tracking: which token each profile is currently scanning ──
_scan_state = {
    "T1": {"token": None, "timestamp": 0},  # Scanner current token
    "T2": {"token": None, "timestamp": 0},  # Lotto current token
    "per_token_spreads": {  # Per-token spread thresholds (default $100)
        "BTC": 100.0, "ETH": 100.0, "SOL": 100.0, "XRP": 100.0,
        "DOGE": 100.0, "BNB": 100.0, "HYPE": 100.0,
    }
}

_CRYPTO_15M_PREFIXES = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXHYPE15M",
                        "KXDOGE15M", "KXBNB15M", "KXXRP15M",
                        "KXBTC30M", "KXETH30M", "KXSOL30M",
                        "KXBTC1H", "KXETH1H", "KXSOL1H")

_btc_cushion_cache = {"val": 0.0, "ts": 0.0, "refreshing": False}

def _token_from_series(series_ticker: str) -> str:
    """Extract token symbol from series ticker (e.g., 'KXBTC15M' -> 'BTC')."""
    if not series_ticker:
        return None
    ticker = series_ticker.upper()
    # Extract token: KXBTC15M -> BTC, KXETH1H -> ETH, KXXRP15M -> XRP, etc.
    if ticker.startswith("KX"):
        rest = ticker[2:]  # Remove 'KX' prefix
        # Remove time suffix (15M, 30M, 1H, 1D, 1W, 1MO, etc.)
        for suffix in ["15M", "30M", "1H", "4H", "1D", "1W", "1MO", "2W"]:
            if rest.endswith(suffix):
                token = rest[:-len(suffix)]
                if len(token) <= 4:  # BTC, ETH, SOL, XRP, DOGE, BNB, HYPE are all ≤4 chars
                    return token
    return None

def _live_token_spread(token: str):
    """Latest live spread (distance from strike) for a token, or None if unknown."""
    td = next((t for t in _token_spread_cache.get("data", [])
               if t.get("symbol") == token), None)
    if td and td.get("spread"):
        return td.get("spread")
    return None

def _effective_safe_threshold(token: str, cfg: dict) -> float:
    """The spread that decides Scanner vs Lotto for this token. When 'Let AI
    choose' is on we use the AI's dynamically-computed safe spread; otherwise the
    user's manual override. Above this spread = safe (Scanner); below = flip-risk
    (Lotto)."""
    if cfg.get("ai_choice", True):
        rec = _safe_spread_cache["data"].get(token)
        if rec is not None:
            return float(rec)
        # AI hasn't computed yet — fall back to the manual/default value below.
    return float(cfg.get("threshold", 100) or 100)

def _check_per_token_filters(market: dict, prof: str) -> tuple:
    """Decide whether `prof` (T1=Scanner / T2=Lotto) should buy this crypto market,
    honoring the per-token Smart Strategy. The token stays ON all the time — what
    changes is WHICH strategy fires:
      • live spread ≥ safe threshold → Scanner (wide cushion, less likely to flip)
      • live spread <  safe threshold → Lotto   (tight, cheap, may flip)
    So with both bots running, each token auto-routes to the right one every cycle.
    Returns (passes, token_symbol)."""
    # Only applies to crypto markets; extract token symbol
    token = _token_from_series(market.get("series_ticker", ""))
    if not token or token not in per_token_settings:
        return (True, token)  # Not a per-token crypto market — leave it alone

    cfg = per_token_settings[token]
    if not cfg.get("enabled", False):
        return (False, token)  # Token off

    # Buy ONLY in the last X minutes of the window (the whole point of the 15-min game)
    buy_last_min = cfg.get("buy_last_min", 15)
    close_time_str = market.get("close_time") or market.get("expiration_time", "")
    if close_time_str:
        try:
            close_time = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
            secs_left = (close_time - datetime.now(timezone.utc)).total_seconds()
            if secs_left < 0:
                return (False, token)  # Already closed
            if secs_left / 60 > buy_last_min:
                return (False, token)  # Too early — outside the buy window
        except Exception:
            pass

    # Route the token to the strategy its live spread calls for.
    prof_strategy = "scanner" if prof == "T1" else ("lotto" if prof == "T2" else None)
    if prof_strategy is None:
        return (True, token)  # Unknown profile — don't gate on strategy

    pinned = cfg.get("mode", "both")
    if pinned in ("scanner", "lotto"):
        wants = pinned  # user pinned this token to one strategy
    else:
        # Auto-switch (default): pick strategy from live spread vs safe threshold.
        current_spread = _live_token_spread(token)
        if current_spread is None:
            return (True, token)  # No live data yet — don't block buying
        threshold = _effective_safe_threshold(token, cfg)
        wants = "scanner" if current_spread >= threshold else "lotto"

    if wants != prof_strategy:
        return (False, token)  # The OTHER bot handles this token right now

    return (True, token)

def _refresh_crypto_spread_bg():
    """Background thread: fetch BTC distance-from-strike and update cache."""
    try:
        spot, _src = _spot_price("BTC")
        if spot:
            cushions = []
            d = kalshi_get("/markets", {"series_ticker": "KXBTC15M", "status": "open", "limit": 5})
            for m in d.get("markets", []):
                strike = _strike_from_market(m)
                if strike:
                    cushions.append(abs(spot - strike))
            if cushions:
                _btc_cushion_cache["val"] = round(sum(cushions) / len(cushions), 2)
                _btc_cushion_cache["ts"] = time.time()
    except Exception as e:
        print(f"[smart-strategy] spread calc failed: {e}")
    finally:
        _btc_cushion_cache["refreshing"] = False

def _average_crypto_spread() -> float:
    """Return cached BTC distance-from-strike immediately; kick a background
    refresh if cache is stale (>30s). Never blocks the caller."""
    now = time.time()
    if (now - _btc_cushion_cache["ts"]) >= 30 and not _btc_cushion_cache["refreshing"]:
        _btc_cushion_cache["refreshing"] = True
        threading.Thread(target=_refresh_crypto_spread_bg, daemon=True).start()
    return _btc_cushion_cache["val"]

_smart_apply_ts = {"t": 0.0}
_per_token_refresh_ts = {"t": 0.0}

def _refresh_per_token_spreads_bg():
    """Refresh the 7 token spreads + AI safe-spread recommendations in the
    background, so the bot's per-token auto-switch has fresh data even when the
    Smart Strategy tab isn't open in any browser."""
    try:
        _token_spread_cache["data"] = _compute_token_spreads()
        _token_spread_cache["ts"] = time.time()
        _compute_safe_spread_recommendations(_token_spread_cache["data"])
    except Exception as e:
        print(f"[per-token] background spread refresh failed: {e}")
    finally:
        _token_spread_cache["refreshing"] = False

def _apply_smart_strategy():
    """Keep the active mode in sync with the live market each cycle.

    Per-token Smart Strategy takes precedence: if ANY token card is ON, we run
    BOTH bots (T1 Scanner + T2 Lotto) and let _check_per_token_filters route each
    token to the right one based on its own live spread vs its safe threshold —
    the token stays on all the time; only the trigger changes. We also keep the
    7 token spreads + AI recommendations fresh here so this works headless.

    If no token cards are on, fall back to the legacy global master switch
    (wide BTC spread → Scanner only, tight → Lotto only)."""
    global active_profiles

    any_token_on = any(c.get("enabled") for c in per_token_settings.values())
    if any_token_on:
        now = time.time()
        # Keep spreads + recommendations fresh (~every 15s) without blocking.
        if (now - _per_token_refresh_ts["t"]) >= 15 and not _token_spread_cache["refreshing"]:
            _per_token_refresh_ts["t"] = now
            _token_spread_cache["refreshing"] = True
            threading.Thread(target=_refresh_per_token_spreads_bg, daemon=True).start()
        # Run BOTH strategies so each token can auto-route to Scanner or Lotto.
        want = ["T1", "T2"]
        if set(active_profiles) != set(want):
            active_profiles = want
            _save_profiles()
            _log("[smart-strategy] per-token active — running Scanner+Lotto, auto-routing each token")
        return

    if not smart_strategy.get("enabled"):
        return
    now = time.time()
    if (now - _smart_apply_ts["t"]) < 60:
        return
    _smart_apply_ts["t"] = now
    spread = _average_crypto_spread()
    if spread <= 0:
        return  # no live data — don't flip modes blind
    want = ["T1"] if spread >= smart_strategy["spread_threshold"] else ["T2"]
    if want != active_profiles:
        active_profiles = want
        _save_profiles()
        _log(f"[smart-strategy] BTC ${spread:,.2f} from strike vs ${smart_strategy['spread_threshold']:,.0f} "
             f"→ switched to {'SCANNER (T1)' if want == ['T1'] else 'LOTTO (T2)'}")

def _ticker_symbol(ticker: str):
    """KXETH15M-26JUN102330-30 → ETH"""
    for pfx in _CRYPTO_15M_PREFIXES:
        if ticker.startswith(pfx):
            sym = pfx[2:]  # strip KX
            for tail in ("15M", "30M", "1H"):
                if sym.endswith(tail):
                    return sym[:-len(tail)]
    return None

def _strike_from_market(m: dict):
    """Pull the strike from a Kalshi market object. Tries the structured fields
    first, then falls back to parsing the dollar amount out of the subtitle."""
    for f in ("floor_strike", "cap_strike", "strike"):
        v = m.get(f)
        if v not in (None, "", 0):
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    import re as _re
    for f in ("yes_sub_title", "subtitle", "title"):
        txt = m.get(f) or ""
        hit = _re.search(r"\$?([\d,]+(?:\.\d+)?)", txt.replace(",", ""))
        if hit:
            try:
                v = float(hit.group(1))
                if v > 0.0001:
                    return v
            except ValueError:
                pass
    return None

# Market data cache — avoid hitting /markets/{ticker} more than once per 60s
_market_cache: dict = {}   # ticker → {"data": {...}, "ts": float}
_MARKET_CACHE_TTL = 60.0   # seconds
_failed_market_cache: set = set()  # tickers that 404/failed — don't retry for 5 min
_failed_market_ts: dict = {}  # ticker → timestamp of failure

# Last good open-positions list — served when a fetch comes back empty while the
# balance API still shows positions (transient empty / the bot starving the API).
_last_positions_cache: dict = {"data": [], "value": 0.0, "ts": 0.0}
# Persist the last-good list to disk so an Update/restart doesn't wipe the fallback
# (the #1 reason the UI showed a blank "No open positions" right after restarting).
POSITIONS_CACHE_FILE = HERE / "positions_cache.json"

def _save_positions_cache():
    try:
        POSITIONS_CACHE_FILE.write_text(json.dumps(_last_positions_cache), encoding="utf-8")
    except Exception as e:
        print(f"[positions-cache] save failed: {e}")

def _load_positions_cache():
    """Load the disk cache into memory if it's newer/non-empty. Tolerates a missing
    or corrupt file (returns quietly). Only used as a fallback on an empty fetch."""
    global _last_positions_cache
    try:
        if POSITIONS_CACHE_FILE.exists():
            d = json.loads(POSITIONS_CACHE_FILE.read_text(encoding="utf-8"))
            if isinstance(d, dict) and d.get("data"):
                _last_positions_cache = d
    except Exception as e:
        print(f"[positions-cache] load failed: {e}")

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
    """Build a PUBLIC Kalshi URL (viewable without logging in).

    The old version used the full event ticker (e.g. 'kxbtc-26jun1217') as the
    slug — that is NOT a valid Kalshi page, so it 404'd / hit the login wall.
    Kalshi's per-market slug isn't exposed by the API, but the SERIES page IS a
    real public page. Series = the first dash-segment of the ticker
    ('KXBTC-26JUN1217-B64250' -> 'KXBTC' -> https://kalshi.com/markets/kxbtc).
    """
    base = (ticker or event_ticker or "").strip()
    if not base:
        return ""
    series = base.split("-", 1)[0]   # first segment = the series ticker
    if not series:
        return ""
    return f"https://kalshi.com/markets/{series.lower()}"

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


def _mark_price_cents(m: dict, side: str) -> float | None:
    """Best 'current' price (cents) for a held contract on `side`.

    Prefer the live bid (the real price you'd get selling). When the book has no
    bid — common for illiquid markets like far-out NBA-draft picks — fall back to
    the last traded price, then the ask, so the Now / P&L columns show a real mark
    instead of a blank "—". Returns None only when nothing is known at all.
    """
    bid = _market_bid(m, side)
    if bid is not None and bid > 0:
        return bid

    # Fall back to last trade. Kalshi's 'last_price' is the YES price; NO = 100 - YES.
    last = m.get("last_price")
    if last is None:
        last = m.get("last_price_dollars")
    try:
        if last is not None:
            yc = float(last)
            if 0 < yc < 1:        # dollar form like 0.12 -> 12¢
                yc *= 100
            if yc >= 1:
                yc = min(99.0, yc)
                return round(yc if side == "yes" else 100 - yc, 2)
    except (TypeError, ValueError):
        pass

    # Last resort: the ask (what it'd cost to buy back) as a rough mark.
    v = _dollars_to_cents(m.get(f"{side}_ask_dollars"))
    if v is not None and v > 0:
        return v

    return bid  # None or 0


def _kalshi_get_with_timeout(endpoint: str, params: dict = None, timeout_secs: float = 5.0) -> dict:
    """Get from Kalshi with a short timeout for monitor thread — skip on timeout."""
    try:
        # Use a separate thread to fetch with timeout
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(kalshi_get, endpoint, params)
            return future.result(timeout=timeout_secs)
    except concurrent.futures.TimeoutError:
        print(f"[monitor] {endpoint} timed out ({timeout_secs}s) — skipping")
        return {}
    except Exception as e:
        print(f"[monitor] {endpoint} error: {e}")
        return {}

def _monitor():
    while True:
        time.sleep(45)
        # Auto-adopt manually bought Kalshi positions into tracked so the sell
        # strategy applies to them too.  Only adds; never overwrites bot entries.
        try:
            port = _kalshi_get_with_timeout("/portfolio/positions", {"limit": 200}, timeout_secs=10.0)
            if not isinstance(port, dict):
                print(f"[monitor] Invalid portfolio response type: {type(port)}")
                continue
            for p in port.get("positions", []):
                if not isinstance(p, dict):
                    print(f"[monitor] Invalid position entry: {type(p)}")
                    continue
                ticker = p.get("market_ticker") or p.get("ticker", "")
                qty    = p.get("position", 0) or 0
                if not ticker or abs(qty) < 0.001:
                    continue
                with _lock:
                    if ticker not in tracked:
                        # Validate qty is positive and non-zero before division
                        if abs(qty) == 0:
                            print(f"[monitor] skipping {ticker}: qty is 0")
                            continue
                        ttd = float(p.get("total_traded_dollars") or 0)
                        raw_price = round(ttd / abs(qty) * 100) if ttd > 0 else None
                        # Ensure buy_price is valid (1-99 cents), not 0
                        buy_price = raw_price if (raw_price and 1 <= raw_price <= 99) else None
                        if buy_price is None:
                            print(f"[monitor] skipping {ticker}: computed buy_price is invalid (raw={raw_price})")
                            continue  # can't auto-sell without a valid cost basis
                        side = "yes" if qty > 0 else "no"
                        tracked[ticker] = {
                            "side":       side,
                            "count":      abs(qty),
                            "buy_price":  buy_price,
                            "title":      ticker,
                            "strategy":   sell_strategy.get("mode", "profit"),
                            "target_pct": sell_strategy.get("target_pct"),
                            "target_dollars": sell_strategy.get("target_dollars"),
                            "bought_at":  None,
                            "status":     "open",
                            "bot_bought": False,
                        }
                        _log(f"[monitor] adopted manual position {ticker} qty={abs(qty)} buy_price={buy_price}¢")
        except Exception as e:
            print(f"[monitor] adopt error: {e}")

        with _lock:
            tickers = list(tracked.keys())

        for ticker in tickers:
            with _lock:
                pos = tracked.get(ticker)
                if not pos or pos.get("status") not in ("open", "selling"):
                    continue
                # Already past its close time — stop probing it every cycle. This was
                # the source of endless "holding to resolution" log spam plus a wasted
                # /markets call per cycle for every expired position (which, with a big
                # tracked file, monopolised the rate-limited API and starved scan/buy).
                if pos.get("expired"):
                    continue
                # Skip only if no profit target exists at all (per-position or global)
                has_pct_target = pos.get("strategy") == "profit" or sell_strategy.get("mode") == "profit"
                has_dol_target = pos.get("target_dollars") is not None or sell_strategy.get("target_dollars") is not None
                if not has_pct_target and not has_dol_target:
                    continue

            try:
                data = _kalshi_get_with_timeout(f"/markets/{ticker}", timeout_secs=5.0)
                if not isinstance(data, dict):
                    print(f"[monitor] Invalid market response for {ticker}: {type(data)}")
                    continue
                m = data.get("market", {})
                if not isinstance(m, dict):
                    print(f"[monitor] Invalid market data for {ticker}")
                    continue
                bid_d = m.get("yes_bid_dollars") if pos["side"] == "yes" else m.get("no_bid_dollars")
                bid = _dollars_to_cents(bid_d)
                if bid is None:
                    continue

                # Guard against division by zero if buy_price is 0 or None
                if not pos.get("buy_price") or pos["buy_price"] <= 0:
                    print(f"[monitor] {ticker}: invalid buy_price {pos.get('buy_price')} — skipping")
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
                    # Skip silently - no buyer at this price, don't log spam
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
                            if mins_left <= 0:
                                # Market is closed (awaiting settlement). Mark it so we
                                # never probe it again — it settles on its own, and the
                                # live Kalshi positions feed still shows it until then.
                                with _lock:
                                    if ticker in tracked:
                                        tracked[ticker]["expired"] = True
                                _save_tracked()
                                print(f"[monitor] {ticker} past close ({mins_left:.1f} min) — done probing, awaiting settlement")
                                continue
                            if mins_left <= threshold:
                                print(f"[monitor] {ticker} expires in {mins_left:.1f} min (<= {threshold}m threshold) — SKIPPING AUTO-SELL (holding to resolution)")
                                continue
                        except Exception as e:
                            print(f"[monitor] Error checking expiration time for {ticker}: {e}")
                            pass

                # Use per-position target if set, otherwise fall back to current global settings
                profit_dollars = pos["count"] * (bid - pos["buy_price"]) / 100 if pos.get("buy_price") else None

                # ENFORCE SINGLE-MODE STRATEGY: only check conditions matching the mode.
                # Use the strategy STAMPED ON THE POSITION at buy time (per-profile),
                # falling back to the global. Before this fix the global T1 mode was
                # applied to every position — so T2 lotto positions meant to ride to
                # resolution were being sold by T1's profit target.
                strat_mode = pos.get("strategy") or sell_strategy.get("mode", "resolution")

                # Initialize all as False; only set based on active mode
                hit_pct = False
                hit_dol = False
                hit_price = False
                hit_stop_pct = False
                hit_stop_dol = False

                # Only check profit % if mode is "profit"
                if strat_mode == "profit":
                    target_pct = pos.get("target_pct") or sell_strategy.get("target_pct")
                    hit_pct = target_pct is not None and profit_pct >= target_pct

                # Only check profit $ if mode is "profit_dollars"
                if strat_mode == "profit_dollars":
                    target_dollars = pos.get("target_dollars") or sell_strategy.get("target_dollars")
                    hit_dol = target_dollars is not None and profit_dollars is not None and profit_dollars >= target_dollars

                # Only check target price if mode is "target_price"
                if strat_mode == "target_price":
                    target_price_c = pos.get("target_price_cents") or sell_strategy.get("target_price_cents")
                    hit_price = target_price_c is not None and bid >= target_price_c

                # Stop-loss applies to all modes — but ONLY if the profile that
                # bought this position has one set RIGHT NOW. Before this fix the
                # global T1 stop-loss was applied to every position, so trades
                # from a profile with stop-loss OFF (unchecked in the UI) were
                # still being stop-lossed by another profile's setting.
                _sl_strat = _sell_profiles.get(pos.get("profile") or "T1") or sell_strategy
                stop_loss_pct = _sl_strat.get("stop_loss_pct")
                stop_loss_dol = _sl_strat.get("stop_loss_dol")
                hit_stop_pct = stop_loss_pct is not None and profit_pct <= -stop_loss_pct
                hit_stop_dol = stop_loss_dol is not None and profit_dollars is not None and profit_dollars <= -stop_loss_dol

                # "resolution" mode: don't auto-sell (skip all conditions above)
                should_sell = (hit_pct or hit_dol or hit_price or hit_stop_pct or hit_stop_dol) if strat_mode != "resolution" else (hit_stop_pct or hit_stop_dol)

                if should_sell:
                    # Prevent double-sell: check if already marked as sold or selling
                    with _lock:
                        if pos.get("status") != "open":
                            print(f"[monitor] {ticker} already {pos.get('status')} — skipping")
                            continue
                        # Mark as selling to prevent concurrent sell requests
                        pos["status"] = "selling"

                    # Build MARKET sell order with protective floor = current bid price
                    bid_key = "yes_bid_dollars" if pos["side"] == "yes" else "no_bid_dollars"
                    bid_d = m.get(bid_key)
                    bid_cents = round(float(bid_d or 0) * 100) if bid_d else 0
                    if bid_cents < 1:
                        print(f"[monitor] {ticker} bid too low to sell ({bid_d}) — skipping")
                        with _lock:
                            if ticker in tracked:
                                tracked[ticker]["status"] = "open"  # revert selling status
                        continue
                    # Kalshi API only accepts whole numbers - round down fractional quantities
                    count_val = int(pos["count"])

                    price_key = "yes_price" if pos["side"] == "yes" else "no_price"
                    order_body = {
                        "ticker":   ticker,
                        "action":   "sell",
                        "side":     pos["side"],
                        "type":     "market",  # MARKET order (matches manual sell behavior)
                        "count":    count_val,
                        "client_order_id": str(uuid.uuid4()),  # Required by Kalshi API
                        price_key:  bid_cents,  # protective floor (cents, integer)
                    }
                    try:
                        result = kalshi_post("/portfolio/orders", order_body)
                        order_status = result.get("order", {}).get("status", "")

                        # Check if order was actually filled (not canceled)
                        if order_status == "canceled":
                            _log(f"[monitor] Auto-sell CANCELED: {pos.get('title', ticker)} | no fill at {bid_cents}¢")
                            with _lock:
                                if ticker in tracked:
                                    tracked[ticker]["status"] = "open"  # revert selling status
                            continue

                        # Use the ACTUAL fill price from the order response, not the
                        # decision-time bid. The old code recorded `bid` as the sell
                        # price — when the book moved between the profit check and the
                        # fill, the UI showed a "profit target" sell that actually
                        # filled lower (the mystery "sold at 84¢, lost 4¢" trades).
                        fill_price = bid
                        try:
                            _ord = result.get("order", {})
                            _fc  = float(_ord.get("taker_fill_count") or 0)
                            _fcost = float(_ord.get("taker_fill_cost") or 0)
                            if _fc > 0 and _fcost > 0:
                                fill_price = round(_fcost / _fc)  # avg fill in cents
                        except Exception:
                            pass
                        # Net P&L after BOTH taker fees (buy + sell) — the gross
                        # number hid that tiny targets often net out negative.
                        cnt_f   = float(pos.get("count") or 0)
                        bp_f    = float(pos.get("buy_price") or 0)
                        gross   = cnt_f * (fill_price - bp_f) / 100
                        fees    = _kalshi_fee(cnt_f, bp_f) + _kalshi_fee(cnt_f, fill_price)
                        net     = round(gross - fees, 2)
                        with _lock:
                            if ticker in tracked:
                                tracked[ticker]["status"]     = "sold"
                                tracked[ticker]["sold_at"]    = datetime.now(timezone.utc).isoformat()
                                tracked[ticker]["sell_price"] = fill_price
                                # Mark whether this was a profit target or stop-loss
                                if hit_stop_pct or hit_stop_dol:
                                    tracked[ticker]["sold_by"] = "bot_stop_loss"
                                else:
                                    tracked[ticker]["sold_by"] = "bot_auto"  # auto-sell by strategy
                        _save_tracked()
                        title = pos.get("title", ticker)
                        if hit_stop_pct or hit_stop_dol:
                            reason = f"STOP-LOSS ({pos.get('profile') or 'T1'} -{stop_loss_pct}%)"
                        else:
                            reason = "PROFIT TARGET"
                        if fill_price != bid:
                            _log(f"[monitor] SLIPPAGE on {ticker}: decision bid {bid}¢ → actual fill {fill_price}¢")
                        _log(f"[monitor] Auto-sold ({reason}): {title} | fill={fill_price}¢ gross=${gross:.2f} fees=${fees:.2f} net=${net:.2f}")
                        _record_activity("sell", ticker=ticker, side=pos.get("side"),
                                         count=pos.get("count"), price=fill_price,
                                         profit=net, profit_gross=round(gross, 2),
                                         fees=round(fees, 2),
                                         profit_pct=round(profit_pct, 1),
                                         sold_by="bot", reason=reason.lower(), title=title,
                                         category=pos.get("category", ""))
                    except Exception as e:
                        # If kalshi_post or result processing fails, revert selling status
                        with _lock:
                            if ticker in tracked:
                                tracked[ticker]["status"] = "open"  # revert selling status
                        raise

            except Exception as e:
                print(f"[monitor] Error checking {ticker}: {e}")

threading.Thread(target=_monitor, daemon=True).start()

# ---------------------------------------------------------------------------
# Bot auto-trading thread (scan/buy loop) — run headless, controlled by /api/bot/start/stop
# ---------------------------------------------------------------------------

_bot_running = False
_bot_start_time = None
_bot_lock = threading.RLock()

BOT_CONFIG_FILE = HERE / "bot_config.json"  # Persists bot state across restarts

def _load_bot_config():
    """Always auto-start the bot on launch.

    The user wants the bot botting whenever the server is up. Stop still works
    during a running session (POST /api/bot/stop), but a fresh start/restart
    always resumes trading so it never silently sits idle after a reboot.
    """
    try:
        _save_bot_config(True)  # persist the running intent
    except Exception as e:
        print(f"[bot config] save error: {e}")
    print("[bot] Auto-starting on launch (always-on policy)")
    return True

def _save_bot_config(should_run: bool):
    """Persist the bot's desired state (so it survives restarts)."""
    try:
        BOT_CONFIG_FILE.write_text(json.dumps({"should_run": should_run}, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[bot config] save error: {e}")

_buy_cooldown: dict = {}   # profile -> epoch time until which buying is paused (out of cash)
_BUY_COOLDOWN_SECS = 60    # after an insufficient_balance, wait this long before trying again

def _scan_and_buy_for_profile(prof, bs, ss, cycle_start):
    """One scan+buy pass for a SINGLE profile's settings. Tags buys with `prof` and
    stamps that profile's sell strategy (`ss`) onto the position. Multiple active
    profiles each get their own pass per cycle so a Lotto bot and the regular bot can
    run side by side. Shares the global rate limiter, so total API traffic stays
    within Kalshi's limits regardless of how many bots are active."""
    up_on   = bool(bs.get("enable_buy_up"))
    down_on = bool(bs.get("enable_buy_down"))
    if not (up_on or down_on):
        _log(f"[bot:{prof}] cycle: skipped — both BUY UP and BUY DOWN are disabled")
        return 0

    # Out-of-cash cooldown: after an insufficient_balance we pause THIS profile's buying
    # for a full minute so we don't keep hammering Kalshi (and tripping 429s) while broke.
    _cd = _buy_cooldown.get(prof, 0)
    if time.time() < _cd:
        _log(f"[bot:{prof}] cycle: skipped — out-of-cash cooldown ({_cd - time.time():.0f}s left)")
        return 0

    up_min,   up_max   = float(bs.get("up_min", 80)),   float(bs.get("up_max", 96))
    down_min, down_max = float(bs.get("down_min", 80)), float(bs.get("down_max", 96))
    minutes  = float(bs.get("minutes", 15))   # may be fractional (0.25–5 custom)
    buy_amt  = float(bs.get("buy_amount", 0.50))
    max_scan = int(bs.get("max_per_scan", 3))
    max_conc = int(bs.get("max_concurrent", 999))
    max_mkt  = int(bs.get("max_per_market", 1))

    mins_, maxs_ = [], []
    if up_on:   mins_.append(up_min);   maxs_.append(up_max)
    if down_on: mins_.append(down_min); maxs_.append(down_max)
    union_min, union_max = min(mins_), max(maxs_)

    now_utc = datetime.now(timezone.utc)
    cutoff  = now_utc + timedelta(minutes=minutes)

    no_crypto    = not bs.get("show_crypto", True)
    no_combo     = not bs.get("show_combo", False)
    no_sports    = not bs.get("show_sports", False)
    no_politics  = not bs.get("show_politics", False)
    no_economics = not bs.get("show_economics", False)
    good_liq     = bool(bs.get("good_liq", True))
    hide_multi   = bool(bs.get("hide_multi", True))
    min_age      = bs.get("min_age_mins")
    max_age      = bs.get("max_age_mins")
    no_buy_within = bs.get("no_buy_within_mins")

    CRYPTO_SERIES = ["KXBTC15M","KXETH15M","KXSOL15M","KXHYPE15M","KXDOGE15M","KXBNB15M","KXXRP15M",
                     "KXBTC30M","KXETH30M","KXSOL30M","KXBTC1H","KXETH1H","KXSOL1H",
                     "KXBTC","KXBTCD","KXBTCW","KXBTCM","KXETH","KXETHUSD","KXETHD",
                     "KXSOL","KXSOLD","KXDOGE","KXXRP"]
    ECON_SERIES   = ["INX","INXD","INXW","KXNDAQ","KXNDAQD","NDX","KXDJIA","DJI",
                     "KXFED","KXCPI","KXPCE","KXUNEMP"]
    SPORTS_SERIES = ["NBAG","KXNBA","NBA","MLBG","KXMLB","MLB","NFLG","KXNFL","NFL",
                     "KXNHL","NHL","KXSOCCER"]
    series_list = []
    if bs.get("show_crypto", True):     series_list += CRYPTO_SERIES
    if bs.get("show_economics", False): series_list += ECON_SERIES
    if bs.get("show_sports", False):    series_list += SPORTS_SERIES

    with _lock:
        open_now = sum(1 for p in tracked.values() if p.get("status") == "open")

    candidates = []  # (ticker, side, price_cents, market)
    scan_errors = 0
    for st in series_list:
        if len(candidates) >= max_scan * 4:
            break
        if time.time() - cycle_start > 12:  # per-profile time budget
            break
        try:
            time.sleep(0.35)  # rate limit (shared limiter keeps us within policy)
            d = kalshi_get("/markets", {"series_ticker": st, "status": "open", "limit": 200})
            for m in d.get("markets", []):
                hit = _apply_market_filters(
                    m, now_utc, cutoff, union_min, union_max,
                    no_crypto, no_combo, no_sports, no_politics, no_economics, good_liq, 10,
                    min_age_mins=min_age, max_age_mins=max_age,
                    no_buy_within_mins=no_buy_within, crypto_times=None, hide_multi=hide_multi)
                if not hit:
                    continue

                # Apply per-token Smart Strategy filters (auto-routes each token to
                # Scanner/Lotto based on its live spread vs its safe threshold).
                passes_token_filter, token_symbol = _check_per_token_filters(m, prof)
                if not passes_token_filter:
                    continue

                ticker = m.get("ticker", "")
                yes_p = _market_price(m, "yes")
                no_p  = _market_price(m, "no")
                if up_on and yes_p is not None and up_min <= yes_p < up_max:
                    candidates.append((ticker, "yes", yes_p, m))
                elif down_on and no_p is not None and down_min <= no_p < down_max:
                    candidates.append((ticker, "no", no_p, m))
        except Exception as e:
            # Was a silent `pass` — a persistent failure here (expired auth, sustained
            # 429s) would make the bot scan fruitlessly for HOURS with no log line.
            # Now surfaced with a timestamp so gaps are diagnosable.
            scan_errors += 1
            if scan_errors <= 3:  # don't spam: first few per cycle is enough
                _log(f"[bot:{prof}] scan error on {st}: {type(e).__name__}: {e}")

    bought = 0
    skipped_too_small = 0
    with _lock:
        for ticker, side, price_c, m in candidates:
            if bought >= max_scan or open_now >= max_conc:
                break
            held = sum(1 for t, p in tracked.items()
                       if t == ticker and p.get("status") == "open")
            if held >= max_mkt:
                continue
            # Never buy the OPPOSITE side of this exact contract — even if our first
            # side was already sold this session. Buying both sides of one market just
            # cancels out (a guaranteed loss on the spread). Applies to ALL bots
            # (Scanner T1, Lotto T2, …) since they share this buy loop. Crypto tickers
            # are unique per 15-min window, so this is effectively per-session there.
            _existing = tracked.get(ticker)
            if _existing and _existing.get("side") not in (None, side):
                continue
            pc = int(math.ceil(price_c))
            if pc <= 0 or pc > 99:
                continue
            contracts = math.floor(buy_amt / (pc / 100))
            if contracts < 1:
                # buy_amount too small for this price (e.g. $0.50 budget at 60¢ → 0
                # contracts). This silently skips EVERY candidate, so the bot can look
                # "stuck" buying nothing. Count it so the cycle summary surfaces it.
                skipped_too_small += 1
                continue

            # Determine order type based on per-token settings (default: market)
            token = _token_from_series(m.get("series_ticker", ""))
            order_type = "market"  # default
            if token and token in per_token_settings:
                order_type = per_token_settings[token].get("order_type", "market")

            order_body = {
                "ticker": ticker,
                "client_order_id": str(uuid.uuid4()),
                "action": "buy",
                "side": side,
                "type": order_type,
                "count": contracts,
            }
            # For both order types, include the price as a limit/reference
            if side == "yes": order_body["yes_price"] = pc
            else:             order_body["no_price"]  = pc
            try:
                result = kalshi_post("/portfolio/orders", order_body)
                order = result.get("order", result)
                if order.get("status") != "rejected":
                    tracked[ticker] = {
                        "title":  _event_title(m.get("event_ticker", "")) or m.get("title", ticker),
                        "category": m.get("category", ""),
                        "side":   side,
                        "count":  contracts,
                        "buy_price":     pc,
                        "current_price": pc,
                        "profit_pct":    0.0,
                        "strategy":      ss.get("mode", "resolution"),
                        "target_pct":    ss.get("target_pct"),
                        "target_dollars": ss.get("target_dollars"),
                        "target_price_cents": ss.get("target_price_cents"),
                        "stop_loss_pct": ss.get("stop_loss_pct"),
                        "status": "open",
                        "bought_at": datetime.now(timezone.utc).isoformat(),
                        "bot_bought": True,
                        "profile": prof,  # which tab's bot bought it (T1/T2/…)
                    }
                    _save_tracked()
                    _balance_cache["ts"] = 0
                    open_now += 1
                    bought += 1
                    # Queue the buy for frontend announcement
                    spent_dollars = round(contracts * pc / 100, 2)
                    _recent_buys_queue.append({
                        "ticker": ticker,
                        "side": side,
                        "count": contracts,
                        "spent": spent_dollars,
                        "category": m.get("category", ""),
                    })
                    _log(f"[bot:{prof}] Auto-bought {side.upper()} {ticker}: {contracts} @ {pc}¢")
                    _record_activity("buy", ticker=ticker, side=side, count=contracts,
                                     price=pc, spent=spent_dollars, profile=prof,
                                     title=m.get("title", ticker), category=m.get("category", ""))
            except Exception as e:
                # Out of cash? Stop trying the rest of this cycle's candidates —
                # otherwise we hammer Kalshi with dozens of doomed orders and trip
                # the 429 rate limiter. Resume next cycle (cash may free up).
                _body = ""
                _resp = getattr(e, "response", None)
                if _resp is not None:
                    try: _body = _resp.text or ""
                    except Exception: _body = ""
                if "insufficient_balance" in (_body + " " + str(e)).lower():
                    _buy_cooldown[prof] = time.time() + _BUY_COOLDOWN_SECS
                    _log(f"[bot:{prof}] out of cash — pausing buys for {_BUY_COOLDOWN_SECS}s")
                    break
                _log(f"[bot:{prof}] Buy {ticker} failed: {e}")
    # Always log a one-line cycle summary so the terminal shows the bot is ALIVE and
    # WHY it bought nothing (no candidates / budget too small / scan errors), with a
    # timestamp. This is what makes a multi-hour gap diagnosable at a glance.
    if bought:
        summary = f"bought {bought} ({len(candidates)} candidates, {open_now} open)"
        _log(f"[bot:{prof}] cycle: {summary}")
    else:
        reasons = []
        if not candidates:        reasons.append("no candidates matched filters")
        if skipped_too_small:     reasons.append(f"{skipped_too_small} skipped (buy_amount too small for price)")
        if scan_errors:           reasons.append(f"{scan_errors} scan errors")
        if open_now >= max_conc:  reasons.append(f"max_concurrent {max_conc} reached")
        why = "; ".join(reasons) or f"{len(candidates)} candidates but none bought"
        summary = f"bought 0 — {why}"
        _log(f"[bot:{prof}] cycle: {summary}")
    # Record every scan cycle so the Summary tab can prove the bot is scanning even
    # when it buys nothing (the missing signal during the 9-hour gap).
    _record_activity("scan", profile=prof, candidates=len(candidates),
                     bought=bought, scan_errors=scan_errors, detail=summary)
    return bought


def _bot_thread():
    """Scan markets and auto-buy every 15s for EACH active profile, respecting each
    profile's own sell strategy. Runs independent of browser; start/stop via
    /api/bot/start and /api/bot/stop."""
    global _bot_running, _bot_start_time, _bot_thread_id

    # Register this thread so all its scan-time Kalshi GETs are auto-deprioritized,
    # letting user-facing requests (positions/settlements/saves) cut ahead.
    _bot_thread_id = threading.get_ident()

    scan_interval = 15  # seconds between scans
    last_scan = 0
    _was_running = None  # track running-state transitions so we log start/stop once

    _log("[bot] trading thread started (waiting for run signal)")

    while True:
        time.sleep(1)  # Check every second if we should scan

        with _bot_lock:
            running = _bot_running
        # Log the moment the bot flips between running and stopped — so a silent
        # /api/bot/stop (which would halt all buying) is visible in the terminal.
        if running != _was_running:
            _log(f"[bot] state → {'RUNNING' if running else 'STOPPED'}")
            _was_running = running
        if not running:
            continue  # Wait for start signal (from API or config file)

        now = time.time()
        if (now - last_scan) < scan_interval:
            continue  # Not time yet

        last_scan = now

        try:
            cycle_start = time.time()
            # Smart Strategy: keep the active mode in sync with the live market
            # (no-op unless enabled; throttled to one check per minute).
            _apply_smart_strategy()
            # Run a scan+buy pass for EACH active profile (multiple bots can run at
            # once — e.g. the regular bot + the Lotto bot). Each uses its own settings.
            profs = list(active_profiles)
            if not profs:
                _log("[bot] cycle: no active profiles — turn on a bot (T1/T2) to trade")
            for prof in profs:
                bs = dict(_profiles.get(prof) or {})
                ss = dict(_sell_profiles.get(prof) or {})
                if bs:
                    _scan_and_buy_for_profile(prof, bs, ss, cycle_start)
        except Exception as e:
            _log(f"[bot] Scan cycle error: {type(e).__name__}: {e}")

# Auto-load bot state from config file (so restarts preserve running state)
if _load_bot_config():
    _bot_running = True
    _bot_start_time = time.time()

# Start the bot thread (enabled/disabled based on config or API calls)
threading.Thread(target=_bot_thread, daemon=True).start()

# ---------------------------------------------------------------------------
# Portfolio snapshots — persisted to file, used for PnL time windows
# ---------------------------------------------------------------------------

SNAPSHOTS_FILE = HERE / "portfolio_snapshots.json"
_snap_lock = threading.RLock()  # Reentrant lock to allow nested acquisitions (e.g., _save_tracked())
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
        # Kalshi field types are fixed (see BUGLOG-001): balance_dollars is dollars,
        # balance is cents, portfolio_value is cents. NEVER use a `> 200` heuristic.
        cash = float(bal_data.get("balance_dollars") or 0)
        if not cash:
            cash = float(bal_data.get("balance") or 0) / 100
        pv   = float(bal_data.get("portfolio_value") or 0) / 100
        total = round(cash + pv, 2)
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
    # Never cache index.html — it changes frequently and a stale cached copy
    # can run old/broken JavaScript even after the file on disk is fixed.
    resp = make_response(send_from_directory(HERE, "index.html"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/mobile")
def mobile():
    # Phone-optimized UI (separate page, same backend/API). Never cache so
    # design tweaks show up immediately on the phone.
    resp = make_response(send_from_directory(HERE, "mobile.html"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/audio/<path:filename>")
def serve_audio(filename):
    # Serves user-supplied sounds (e.g. audio/chaching.mp3) committed to the repo.
    return send_from_directory(HERE / "audio", filename)


@app.route("/photos/<path:filename>")
def serve_photos(filename):
    return send_from_directory(HERE / "photos", filename)


# ── PWA: makes KalshiBot installable as a standalone app (icon + own window) ──
@app.route("/manifest.json")
def pwa_manifest():
    return send_from_directory(HERE, "manifest.json", mimetype="application/manifest+json")

@app.route("/sw.js")
def pwa_sw():
    resp = make_response(send_from_directory(HERE, "sw.js", mimetype="application/javascript"))
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp

@app.route("/icons/<path:filename>")
def serve_icons(filename):
    return send_from_directory(HERE / "icons", filename)


@app.route("/api/debug")
def debug():
    data = kalshi_get("/markets", {"status": "open", "limit": 3})
    return jsonify(data.get("markets", []))

@app.route("/api/debug/balance")
def debug_balance():
    # Cached raw balance (shared cache; same shape the /mobile page expects).
    b = _get_balance()
    return jsonify(b["raw"] if b else {})


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


# ── Balance cache ──────────────────────────────────────────────────────────
# Cash is fetched by both /api/portfolio and /api/balance, cached briefly so a
# burst of refreshes (or a scan hogging the API lock) doesn't re-hit Kalshi each
# time. On a failed refresh we serve the LAST KNOWN value instead of None, so the
# cash figure never blanks back to "—" once it has loaded.
_balance_cache = {"data": None, "ts": 0.0}
_BALANCE_TTL = 12.0  # seconds

def _get_balance(force: bool = False):
    """Return {cash, positions_value, total, raw} or None (only if never fetched)."""
    now = time.time()
    cached = _balance_cache["data"]
    if not force and cached is not None and (now - _balance_cache["ts"]) < _BALANCE_TTL:
        return cached
    try:
        bal_data = kalshi_get("/portfolio/balance")
        # Try multiple field names for cash (API may have changed)
        cash_dollars = None
        if "balance_dollars" in bal_data:
            cash_dollars = float(bal_data.get("balance_dollars") or 0)
        if cash_dollars is None or cash_dollars == 0:
            # Fallback to cents field if dollars not present or zero
            balance_cents = bal_data.get("balance") or 0
            if balance_cents:
                cash_dollars = float(balance_cents) / 100
            else:
                cash_dollars = 0.0

        pos_dollars = round(float(bal_data.get("portfolio_value") or 0) / 100, 2)

        # Debug log the response if cash is unexpectedly zero
        if cash_dollars == 0 and ("balance" in bal_data or "balance_dollars" in bal_data):
            print(f"[balance] API response: {bal_data}")

        data = {
            "cash":            round(cash_dollars, 2),
            "positions_value": pos_dollars,
            "total":           round(cash_dollars + pos_dollars, 2),
            "raw":             bal_data,
        }
        _balance_cache["data"] = data
        _balance_cache["ts"]   = now
        _balance_cache["live"] = True            # fresh, real Kalshi data
        _balance_cache["last_ok"] = now
        return data
    except Exception as e:
        print(f"[balance] fetch error: {e} (serving cached={cached is not None})")
        _balance_cache["live"] = False           # serving STALE — Kalshi unreachable
        return cached  # last-known on failure; None only if we never succeeded


@app.route("/api/system-info")
def system_info():
    """Return system information (username, hostname, OS, etc.) for onboarding and diagnostics."""
    import platform
    username = os.getenv("USERNAME", "").strip() or socket.gethostname()
    return jsonify({
        "username": username,
        "hostname": socket.gethostname(),
        "platform": platform.system(),
    })


# ── Smart Strategy token cards: cached live spreads ─────────────────────────
_SMART_TOKENS = {
    "BTC": "KXBTC15M", "ETH": "KXETH15M", "SOL": "KXSOL15M",
    "XRP": "KXXRP15M", "DOGE": "KXDOGE15M", "BNB": "KXBNB15M",
    "HYPE": "KXHYPE15M",
}
_token_spread_cache = {"data": [], "ts": 0.0, "refreshing": False}
_TOKEN_SPREAD_TTL = 12.0  # seconds — short so the spread tracks the market

def _fetch_one_token_spread(symbol, series_prefix):
    """Live 'spread' for one token = how far spot is from the nearest strike
    (how far it is from 'flipping'). Returns both:
      • spread        — blended over the nearest open markets (the live headline)
      • nearest_*     — the SOONEST-closing game's own spread + seconds-to-close,
                        so we can record the spread in its final minute (the moment
                        that actually decides a flip, since the bot buys at the end)."""
    try:
        spot, _src = _spot_price(symbol)
        d = kalshi_get("/markets", {"series_ticker": series_prefix,
                                    "status": "open", "limit": 5})
        markets = (d or {}).get("markets", [])
        if not markets:
            return {"symbol": symbol, "spread": 0, "spot": 0, "volume": 0, "message": "No open markets"}
        cushions = []
        total_vol = 0
        nearest = None  # (secs_to_close, close_ts, spread, strike) for soonest game
        now_ts = time.time()
        for m in markets:
            strike = _strike_from_market(m)
            cush = abs(spot - strike) if (strike and spot) else None
            if cush is not None:
                cushions.append(cush)
            total_vol += int(m.get("volume", 0) or 0)
            # Track the soonest-closing game so we can capture its closing spread.
            ct_str = m.get("close_time") or m.get("expiration_time") or ""
            if cush is not None and ct_str:
                try:
                    close_ts = datetime.fromisoformat(ct_str.replace("Z", "+00:00")).timestamp()
                    secs = close_ts - now_ts
                    if secs > 0 and (nearest is None or secs < nearest[0]):
                        nearest = (secs, close_ts, cush, strike)
                except Exception:
                    pass
        if not spot:
            return {"symbol": symbol, "spread": 0, "spot": 0, "volume": total_vol, "message": "No spot price"}
        if not cushions:
            return {"symbol": symbol, "spread": 0, "spot": round(spot, 6), "volume": total_vol, "message": "No strike data"}
        out = {"symbol": symbol, "spread": round(sum(cushions) / len(cushions), 2),
               "spot": round(spot, 6), "volume": total_vol, "ticker": markets[0].get("ticker", "")}
        if nearest:
            out["nearest_secs"] = round(nearest[0])
            out["nearest_close_ts"] = nearest[1]
            out["nearest_spread"] = round(nearest[2], 4)
            out["nearest_strike"] = round(nearest[3], 6)  # the "To Beat" price
        return out
    except Exception as e:
        return {"symbol": symbol, "spread": 0, "spot": 0, "volume": 0, "message": f"error: {e}"}

def _compute_token_spreads():
    """Fetch all 7 token spreads concurrently, sorted in display order."""
    result = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=7) as executor:
        futures = {executor.submit(_fetch_one_token_spread, sym, prefix): sym
                   for sym, prefix in _SMART_TOKENS.items()}
        for future in concurrent.futures.as_completed(futures):
            try:
                result.append(future.result())
            except Exception:
                pass
    order = list(_SMART_TOKENS.keys())
    result.sort(key=lambda t: order.index(t.get("symbol")) if t.get("symbol") in order else 999)
    return result

# ── Per-GAME spread history ─────────────────────────────────────────────────
# Every crypto market is a fresh 15-min GAME. We keep ONE representative spread
# per game (the latest reading in that 15-min window — i.e. closest to its close,
# which is when the bot actually buys), then compare the live spread to the
# trailing average ACROSS games. 4h = 16 games, 24h = 96 games. This is the
# reference the user reasons in: "current spread vs the last 16 spreads".
_GAME_SECS  = 900    # 15-minute game
_MAX_GAMES  = 96     # keep 24h of games
_GAMES_4H   = 16     # 4 hours = 16 games
_CLOSING_WINDOW = 90 # only record a game's spread within the last 90s before close
_token_game_spreads = {sym: {} for sym in _SMART_TOKENS}  # {game_bucket: closing_spread}

def _round_spread(v: float) -> float:
    """Round a spread to a sensible precision for its MAGNITUDE — not by token
    name. BTC's distance-from-strike is ~$100s (whole dollars), DOGE's is fractions
    of a cent (needs 4 decimals). A fixed rule would zero-out the small tokens."""
    if v < 0.1:   return round(v, 4)
    if v < 1:     return round(v, 3)
    if v < 100:   return round(v, 2)
    return round(v)

def _record_closing_spread(symbol: str, close_ts: float, spread: float):
    """Record a game's spread in its FINAL MINUTE, bucketed by the game's own close
    time so each 15-min game stores exactly one value — the spread right before it
    closed (when the bot buys and a flip is decided). Later reads (even closer to
    close) overwrite, so we keep the tightest pre-close reading."""
    buckets = _token_game_spreads.setdefault(symbol, {})
    b = int(close_ts // _GAME_SECS)
    buckets[b] = spread
    if len(buckets) > _MAX_GAMES:                      # prune oldest games
        for old in sorted(buckets)[:-_MAX_GAMES]:
            del buckets[old]
    _save_game_spreads()                               # persist (throttled)

def _recent_game_spreads(symbol: str, games: int, exclude_bucket=None):
    """The last `games` per-game spreads, oldest→newest.

    Pass exclude_bucket to drop the CURRENT (still-open) game's bucket. We record
    a game's spread in its final 90s, so without this the in-progress game would
    fold into its OWN threshold right at buy/trigger time — making the line move
    during the game. Excluding it keeps the threshold FIXED for the whole 15-min
    game, so the live spread can actually cross it and trigger a mode switch."""
    buckets = _token_game_spreads.get(symbol, {})
    keys = [k for k in sorted(buckets) if k != exclude_bucket]
    return [buckets[k] for k in keys[-games:]]

def _avg_spread_games(symbol: str, games: int = _GAMES_4H):
    """(avg_spread, games_counted) over the last `games` games, or (None, 0)."""
    vals = _recent_game_spreads(symbol, games)
    if not vals:
        return (None, 0)
    return (_round_spread(sum(vals) / len(vals)), len(vals))

def _spread_volatility(symbol: str, games: int = _GAMES_4H):
    """(std, cv, elevated) over the last `games` games — how much the spread is
    bouncing around. cv = std/avg, which is comparable across tokens of different
    price magnitude (a 30% swing means the same for BTC and DOGE). 'elevated' is a
    heads-up that the token is jumping more than normal, so a position could flip
    faster than usual. (None, None, False) until enough games are logged."""
    vals = _recent_game_spreads(symbol, games)
    if len(vals) < 3:
        return (None, None, False)
    avg = sum(vals) / len(vals)
    std = (sum((x - avg) ** 2 for x in vals) / len(vals)) ** 0.5
    cv = (std / avg) if avg else 0.0
    latest = vals[-1]
    # Elevated when swings are large vs the average (>25%) OR the latest game
    # jumped more than 2 std away from the recent norm.
    elevated = cv > 0.25 or (std > 0 and abs(latest - avg) > 2 * std)
    return (_round_spread(std), round(cv, 3), elevated)

# ── Persistence: never lose backtest data across restarts ──────────────────
# The per-game spread history IS our backtest dataset, so we keep it on disk in
# TWO places — if one is wiped, the other survives:
#   1) backtest_data/ inside the repo   → committed to GitHub (off-site backup)
#   2) ~/KalshiBots/backtest_data/      → on THIS pc but OUTSIDE the repo, so a
#      re-clone / git clean can't take it. Path.home() resolves to whoever is
#      running the bot (C:\Users\<you> on Windows, /home/<you> on Linux/Mac) —
#      never a hard-coded machine name or username.
BACKTEST_DIR         = HERE / "backtest_data"
HOME_BACKTEST_DIR    = Path.home() / "KalshiBots" / "backtest_data"
_SPREAD_HISTORY_NAME = "game_spread_history.json"
_SPREAD_SAVE_EVERY   = 20          # seconds — throttle disk writes
_spread_save_state   = {"ts": 0.0}

def _spread_history_paths():
    """Both backup locations: repo copy (→GitHub) first, home copy second."""
    return [BACKTEST_DIR / _SPREAD_HISTORY_NAME,
            HOME_BACKTEST_DIR / _SPREAD_HISTORY_NAME]

def _save_game_spreads(force: bool = False):
    """Write the per-game spread history to disk (repo + home backup).
    Throttled so the closing-window writes don't hammer the disk."""
    now = time.time()
    if not force and (now - _spread_save_state["ts"]) < _SPREAD_SAVE_EVERY:
        return
    _spread_save_state["ts"] = now
    # JSON object keys must be strings, so bucket ints → str on save.
    payload = {
        "version": 1,
        "saved_at": now,
        "game_secs": _GAME_SECS,
        "spreads": {sym: {str(b): v for b, v in buckets.items()}
                    for sym, buckets in _token_game_spreads.items()},
    }
    blob = json.dumps(payload, indent=2)
    for p in _spread_history_paths():
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(blob, encoding="utf-8")
        except Exception as e:
            print(f"[backtest] save to {p} failed: {e}")

def _load_game_spreads():
    """Restore per-game spread history on startup. Prefer whichever copy
    (repo or home backup) was saved most recently, so the freshest data wins."""
    best = None  # (saved_at, parsed_dict)
    for p in _spread_history_paths():
        try:
            if p.exists():
                d = json.loads(p.read_text(encoding="utf-8"))
                ts = float(d.get("saved_at", 0))
                if best is None or ts > best[0]:
                    best = (ts, d)
        except Exception as e:
            print(f"[backtest] load from {p} failed: {e}")
    if not best:
        return
    for sym, buckets in (best[1].get("spreads") or {}).items():
        dst = _token_game_spreads.setdefault(sym, {})
        for b, v in buckets.items():
            try:
                dst[int(b)] = float(v)
            except (ValueError, TypeError):
                continue
    # Trim to the retention window in case an older file held more games.
    for buckets in _token_game_spreads.values():
        if len(buckets) > _MAX_GAMES:
            for old in sorted(buckets)[:-_MAX_GAMES]:
                del buckets[old]
    n = sum(len(b) for b in _token_game_spreads.values())
    print(f"[backtest] restored {n} game spreads from disk")

_load_game_spreads()

# ── First-run seeder: smart start instead of ~4h cold start ─────────────────
# A brand-new install has no spread history, so without this it would use a
# cautious fallback for ~4h before the data-driven threshold kicks in. Instead we
# reconstruct the last day of per-game spreads from PUBLIC market history so the
# threshold is meaningful from minute one. We rebuild the SAME metric the live bot
# records — distance from the underlying's close price to the nearest strike of
# that game — using Coinbase 15-min closes + Kalshi settled strikes. It uses only
# public market data (never personal trades) and is fully best-effort: any failure
# just leaves the normal cold-start fallback in place. Runs once, in the
# background, only when there's no history yet.
_COINBASE_CANDLE = {  # tokens with a Coinbase USD candle feed (others skip → fallback)
    "BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD",
    "XRP": "XRP-USD", "DOGE": "DOGE-USD",
}

def _coinbase_closes_by_bucket(symbol):
    """{game_bucket: close_price} for the last ~24h of 15-min candles, or {}."""
    prod = _COINBASE_CANDLE.get(symbol)
    if not prod:
        return {}
    try:
        r = req.get(f"https://api.exchange.coinbase.com/products/{prod}/candles",
                    params={"granularity": _GAME_SECS}, timeout=8)
        if not r.ok:
            return {}
        out = {}
        for row in r.json():
            # Coinbase candle = [start_ts, low, high, open, close, volume].
            # The close at start_ts+900 is the price at the game's CLOSE, so it
            # buckets to the same game the live bot would have recorded.
            start_ts = int(row[0]); close_px = float(row[4])
            close_ts = start_ts + _GAME_SECS
            out[int(close_ts // _GAME_SECS)] = close_px
        return out
    except Exception:
        return {}

def _kraken_closes_by_bucket(symbol):
    """Kraken OHLC fallback for historical closes (same public source the spot
    price already falls back to), so the seeder doesn't depend on the Coinbase
    exchange host being reachable. {game_bucket: close_price} or {}."""
    pair = _KRAKEN_PAIRS.get(symbol)
    if not pair:
        return {}
    try:
        r = req.get("https://api.kraken.com/0/public/OHLC",
                    params={"pair": pair, "interval": _GAME_SECS // 60}, timeout=8)
        if not r.ok:
            return {}
        res = (r.json() or {}).get("result", {})
        # result holds the pair series plus a "last" cursor; pick the list value.
        series = next((v for k, v in res.items() if k != "last" and isinstance(v, list)), None)
        if not series:
            return {}
        out = {}
        for row in series:
            # Kraken OHLC = [time, open, high, low, close, vwap, volume, count].
            start_ts = int(row[0]); close_px = float(row[4])
            close_ts = start_ts + _GAME_SECS
            out[int(close_ts // _GAME_SECS)] = close_px
        return out
    except Exception:
        return {}

def _underlying_closes_by_bucket(symbol):
    """Historical 15-min closes for a token — Coinbase first, Kraken fallback."""
    return _coinbase_closes_by_bucket(symbol) or _kraken_closes_by_bucket(symbol)

def _settled_strikes_by_bucket(series_prefix):
    """{game_bucket: [strike, ...]} from recently-settled Kalshi markets, or {}."""
    since = int(time.time() - 24 * 3600)
    out = {}
    try:
        d = kalshi_get("/markets", {"series_ticker": series_prefix,
                                    "status": "settled",
                                    "min_close_ts": since,
                                    "limit": 1000})
        for m in (d or {}).get("markets", []):
            strike = _strike_from_market(m)
            ct_str = m.get("close_time") or m.get("expiration_time") or ""
            if not (strike and ct_str):
                continue
            try:
                close_ts = datetime.fromisoformat(ct_str.replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            out.setdefault(int(close_ts // _GAME_SECS), []).append(strike)
    except Exception:
        pass
    return out

def _seed_game_spreads_from_history():
    """Reconstruct recent per-game spreads from public market history (first run)."""
    seeded = 0
    for symbol, prefix in _SMART_TOKENS.items():
        try:
            closes = _underlying_closes_by_bucket(symbol)
            if not closes:
                continue
            strikes = _settled_strikes_by_bucket(prefix)
            if not strikes:
                continue
            dst = _token_game_spreads.setdefault(symbol, {})
            for b in sorted(set(closes) & set(strikes))[-_MAX_GAMES:]:
                spot = closes[b]
                # Nearest strike = the truest "how close to flipping" cushion.
                dst[b] = _round_spread(min(abs(spot - k) for k in strikes[b]))
                seeded += 1
        except Exception as e:
            print(f"[backtest] seed {symbol} failed: {e}")
    if seeded:
        _save_game_spreads(force=True)
        print(f"[backtest] first-run seed: filled {seeded} games from market history")
    else:
        print("[backtest] first-run seed: no public history available, using cold-start fallback")

def _maybe_seed_game_spreads():
    """Run the seeder once, in the background, only when we have no history yet."""
    if any(_token_game_spreads.get(s) for s in _SMART_TOKENS):
        return
    threading.Thread(target=_seed_game_spreads_from_history, daemon=True).start()

_maybe_seed_game_spreads()

# ── Trade-outcome learning: tune aggressiveness from the user's OWN results ──
# Each user's bot learns from their own settled win/loss outcomes (derived from
# the local activity log — never shared, never anyone else's). A poor recent
# win-rate nudges that token MORE conservative (favor Scanner/safe); a strong
# win-rate nudges it slightly MORE aggressive (allow Lotto a bit more). The nudge
# is a small, capped multiplier on the safe-spread threshold and stays NEUTRAL
# until there are enough of the user's own outcomes — so a fresh install is
# unaffected and nothing drastic can ever happen (downloaders can't edit code).
TRADE_LEARNING_NAME = "trade_learning.json"
_LEARN_MIN_SAMPLES = 5      # need at least this many of the user's outcomes first
_LEARN_WINDOW      = 20     # judge on the last N outcomes per token
_LEARN_MAX_ADJ     = 0.15   # threshold nudge capped at ±15% — safe by design
_trade_learning = {"by_token": {}, "saved_at": 0.0}
_learn_refresh_state = {"ts": 0.0}
_LEARN_REFRESH_EVERY = 120  # seconds

def _trade_learning_paths():
    return [BACKTEST_DIR / TRADE_LEARNING_NAME, HOME_BACKTEST_DIR / TRADE_LEARNING_NAME]

def _save_trade_learning():
    blob = json.dumps({"by_token": _trade_learning["by_token"],
                       "saved_at": time.time()}, indent=2)
    for p in _trade_learning_paths():
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(blob, encoding="utf-8")
        except Exception as e:
            print(f"[learning] save to {p} failed: {e}")

def _load_trade_learning():
    best = None
    for p in _trade_learning_paths():
        try:
            if p.exists():
                d = json.loads(p.read_text(encoding="utf-8"))
                ts = float(d.get("saved_at", 0))
                if best is None or ts > best[0]:
                    best = (ts, d)
        except Exception as e:
            print(f"[learning] load from {p} failed: {e}")
    if best:
        _trade_learning["by_token"] = best[1].get("by_token", {}) or {}
        _trade_learning["saved_at"] = best[0]

def _ticker_token(ticker: str):
    """Map a Kalshi ticker (e.g. KXBTC15M-...) back to its token symbol."""
    for tok, prefix in _SMART_TOKENS.items():
        if ticker.startswith(prefix):
            return tok
    return None

def _refresh_trade_learning(force: bool = False):
    """Recompute per-token recent win-rate from the user's OWN settled sells in the
    activity log. Cheap, local-only, throttled. Never touches the trading path."""
    now = time.time()
    if not force and (now - _learn_refresh_state["ts"]) < _LEARN_REFRESH_EVERY:
        return
    _learn_refresh_state["ts"] = now
    try:
        events = _read_activity(now - 30 * 24 * 3600)
    except Exception:
        return
    per = {}  # token → list of 1(win)/0(loss), oldest→newest
    for e in events:
        if e.get("kind") != "sell":
            continue
        tok = _ticker_token(e.get("ticker", "") or "")
        profit = e.get("profit")
        if not tok or profit is None:
            continue
        try:
            per.setdefault(tok, []).append(1 if float(profit) > 0 else 0)
        except (TypeError, ValueError):
            continue
    out = {}
    for tok, wl in per.items():
        recent = wl[-_LEARN_WINDOW:]
        n = len(recent); wins = sum(recent)
        out[tok] = {"n": n, "wins": wins, "losses": n - wins,
                    "rate": round(wins / n, 3) if n else None}
    _trade_learning["by_token"] = out
    _save_trade_learning()

def _learning_multiplier(symbol: str) -> float:
    """Bounded threshold nudge from this user's own results, in
    [1-_LEARN_MAX_ADJ, 1+_LEARN_MAX_ADJ]. 1.0 (neutral) until enough samples.
    Higher safe line → Lotto used a bit more (aggressive); lower → Scanner favored
    (conservative). Win-rate 0.50 = neutral, 0.60 = max aggressive, 0.40 = max
    conservative."""
    rec = _trade_learning.get("by_token", {}).get(symbol)
    if not rec or rec.get("rate") is None or rec.get("n", 0) < _LEARN_MIN_SAMPLES:
        return 1.0
    adj = (rec["rate"] - 0.50) / 0.10 * _LEARN_MAX_ADJ
    return 1.0 + max(-_LEARN_MAX_ADJ, min(_LEARN_MAX_ADJ, adj))

_load_trade_learning()

def _calculate_safe_spread(symbol: str, current_spread: float, exclude_bucket=None) -> float:
    """AI-recommended safe spread = 75th percentile of recent closing spreads.

    This threshold is FIXED (doesn't chase current spread) but adapts to volatility:
    • Above threshold (top 25% of recent spreads) → Scanner (historically safe, wide)
    • Below threshold (bottom 75% of recent spreads) → Lotto (historically tight, risky)

    Avoids the earlier circular problem where avg+1.5σ trapped us in Lotto ~93% of
    the time because current spread IS drawn from that same distribution.

    Computed from the last 16 COMPLETED games (4h) — the current in-progress game
    is excluded (see exclude_bucket) so the line holds steady for the whole game.
    Scales to each token's magnitude automatically."""
    # Bounded nudge from the user's own win/loss history (1.0 until enough data).
    learn = _learning_multiplier(symbol)
    vals = _recent_game_spreads(symbol, _GAMES_4H, exclude_bucket=exclude_bucket)
    if len(vals) < 3:
        # Not enough games yet — cushion proportional to the live spread (25% over).
        # No absolute floor, so cent-scale (DOGE/XRP) and dollar-scale (BTC/ETH)
        # tokens both get a sane starting threshold.
        return _round_spread(current_spread * 1.25 * learn)

    # Use 75th percentile as the safe threshold. Spreads above this are in the
    # safest top 25% of recent history, giving confidence against flips.
    vals_sorted = sorted(vals)
    idx = int(len(vals_sorted) * 0.75)
    if idx >= len(vals_sorted):
        idx = len(vals_sorted) - 1
    safe = vals_sorted[idx]

    return _round_spread(safe * learn)

_safe_spread_cache = {"data": {}, "ts": 0.0}

def _compute_safe_spread_recommendations(token_spreads):
    """Given current token spreads, compute safe spread recommendations for each.
    Returns dict: {BTC: rec_spread, ETH: rec_spread, ...}"""
    _refresh_trade_learning()  # throttled; keeps the per-token win-rate nudge current
    recommendations = {}
    for token_data in token_spreads:
        symbol = token_data.get("symbol")
        spread = token_data.get("spread", 0)
        if not symbol:
            continue
        # Record the per-GAME history ONLY in the final minute before a game closes,
        # so each game stores the spread at the moment the bot buys (when a flip is
        # decided) — not a blend across the whole 15 minutes.
        nsecs = token_data.get("nearest_secs")
        nclose = token_data.get("nearest_close_ts")
        nspread = token_data.get("nearest_spread")
        if (nspread and nclose and nsecs is not None and 0 <= nsecs <= _CLOSING_WINDOW):
            _record_closing_spread(symbol, nclose, nspread)
        if spread > 0:
            # Exclude the current (still-open) game from its own threshold so the
            # line stays FIXED for the whole 15-min game and the live spread can
            # cross it to trigger a Scanner/Lotto switch.
            cur_bucket = int(nclose // _GAME_SECS) if nclose else None
            recommendations[symbol] = _calculate_safe_spread(symbol, spread, exclude_bucket=cur_bucket)
    if recommendations:
        # Cache so the bot's auto-switch can read fresh thresholds without the UI open.
        _safe_spread_cache["data"] = recommendations
        _safe_spread_cache["ts"] = time.time()
    return recommendations


@app.route("/api/learning")
def learning_status():
    """Transparency: this user's own per-token win-rate and the resulting bounded
    threshold nudge. All local — never shared. Lets the UI show why a token is
    being played a little more/less aggressively."""
    _refresh_trade_learning()
    out = {}
    for tok in _SMART_TOKENS:
        rec = _trade_learning.get("by_token", {}).get(tok, {})
        mult = _learning_multiplier(tok)
        if mult > 1.001:
            tilt = "more aggressive"
        elif mult < 0.999:
            tilt = "more conservative"
        else:
            tilt = "neutral"
        out[tok] = {"n": rec.get("n", 0), "wins": rec.get("wins", 0),
                    "losses": rec.get("losses", 0), "win_rate": rec.get("rate"),
                    "threshold_multiplier": round(mult, 3), "tilt": tilt}
    return jsonify({"min_samples": _LEARN_MIN_SAMPLES, "window": _LEARN_WINDOW,
                    "max_adjust_pct": int(_LEARN_MAX_ADJ * 100), "tokens": out})


@app.route("/api/smart-strategy/tokens")
def smart_strategy_tokens():
    """Per-token live spread for the Smart Strategy cards. Served from a short
    cache: the first call computes synchronously (slow — 7 spot+market lookups),
    later calls are instant and a background thread keeps the data fresh so the
    spread tracks live price movements."""
    now = time.time()
    fresh = (now - _token_spread_cache["ts"]) < _TOKEN_SPREAD_TTL
    have_data = bool(_token_spread_cache["data"])

    if not fresh and not _token_spread_cache["refreshing"]:
        _token_spread_cache["refreshing"] = True
        if have_data:
            # Serve the (slightly stale) cache instantly, refresh in background.
            def _bg():
                try:
                    _token_spread_cache["data"] = _compute_token_spreads()
                    _token_spread_cache["ts"] = time.time()
                except Exception as e:
                    print(f"[smart-strategy] token refresh failed: {e}")
                finally:
                    _token_spread_cache["refreshing"] = False
            threading.Thread(target=_bg, daemon=True).start()
        else:
            # First ever call — compute now so the cards have data to show.
            try:
                _token_spread_cache["data"] = _compute_token_spreads()
                _token_spread_cache["ts"] = time.time()
            finally:
                _token_spread_cache["refreshing"] = False

    return jsonify({
        "tokens": _token_spread_cache["data"],
        "timestamp": _token_spread_cache["ts"],
        "note": "Spread = distance from the strike (how far the token is from flipping)",
    })


@app.route("/api/balance")
def balance_only():
    """Lightweight cash + positions value — one (cached) Kalshi call. The frontend
    hits this so the top cash figure shows immediately, decoupled from the slow
    positions/enrichment load that can get stuck behind a scan."""
    b = _get_balance()
    if not b:
        return jsonify({"balance": None, "live": False})
    # live=False means the last Kalshi balance fetch FAILED and we're serving the
    # last-known value — so a "$0" here may be stale, not real. The UI flags it.
    now = time.time()
    live = bool(_balance_cache.get("live", True))
    age = now - _balance_cache.get("last_ok", now)
    return jsonify({
        "balance":         b["cash"],
        "positions_value": b["positions_value"],
        "portfolio_value": b["total"],
        "live":            live,
        "age_seconds":     round(age),
    })


def _position_age_seconds(info):
    """Seconds since a tracked position was bought, or None if unknown."""
    ts = info.get("bought_at")
    if not ts:
        return None
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
    except Exception:
        return None


@app.route("/api/portfolio")
def portfolio():
    # Check if we should skip expensive market enrichment (for fast initial load)
    enrich = request.args.get("enrich", "true").lower() != "false"

    # ── Balance ── (shared cache so cash shows fast even mid-scan; see _get_balance)
    balance = None
    total_account = None  # total account value (what Kalshi shows as Portfolio)
    bal_data = {}         # kept for the portfolio_value fallback below
    _b = _get_balance()
    if _b:
        balance       = _b["cash"]    # spendable cash (balance_dollars)
        total_account = _b["total"]   # cash + open positions
        bal_data      = _b["raw"]

    # ── Positions ──
    positions = []
    portfolio_value = 0.0
    positions_ok = True  # did Kalshi's live positions fetch succeed this call?
    # Kalshi's own open-positions value (from the balance endpoint). Computed up
    # front so the positions fetch below can RETRY when it comes back empty but
    # Kalshi says we actually hold positions (i.e. the fetch was just starved).
    api_positions_value = 0.0
    try:
        _pv_raw = float(bal_data.get("portfolio_value", 0))
        if _pv_raw > 0:
            api_positions_value = round(_pv_raw / 100, 2)  # always cents (BUGLOG-001)
    except Exception:
        pass
    _buy_ts_by_ticker = {}  # ticker → most-recent BUY fill time (defined before the
                            # try so the tracked-fallback block below is always safe)
    try:
        # Fetch ALL open positions with cursor pagination. If the first attempt
        # comes back empty WHILE the balance API says we hold open positions, the
        # fetch was starved by the scan loop — retry a few times (threaded server
        # lets these run concurrently) so a transient empty never blanks the list.
        raw_positions = []
        for _attempt in range(3):
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
            # Got data, or Kalshi says we genuinely hold nothing → stop retrying.
            if raw_positions or api_positions_value <= 0:
                break
            time.sleep(0.4)  # brief backoff, then re-fetch (high-priority UI call)

        # Real "bought at" fallback from Kalshi fills, for positions the bot didn't
        # record itself (manual/adopted). Without this, bought_at is blank → the
        # "Most Recent" sort breaks and the Bought At column shows "—". Use the most
        # recent BUY fill per ticker (ISO strings compare correctly lexicographically).
        _buy_ts_by_ticker = {}
        try:
            for f in (_cached_fills_nonblocking() or []):
                if str(f.get("action", "")).lower() != "buy":
                    continue
                tk = f.get("ticker", "")
                ct = f.get("created_time")
                if not tk or not ct:
                    continue
                if tk not in _buy_ts_by_ticker or str(ct) > str(_buy_ts_by_ticker[tk]):
                    _buy_ts_by_ticker[tk] = ct
        except Exception as e:
            print(f"[portfolio] fills bought_at fallback failed: {e}")

        # PERF: warm the market cache for every held ticker in ONE batched call
        # (GET /markets?tickers=A,B,C, ≤100 each) instead of a serialized
        # /markets/{ticker} per position below. The per-position _get_market() then
        # just hits this warm cache — no per-position API calls monopolizing the
        # rate limiter. Fully additive: if the batch fails, the per-position path
        # still works exactly as before.
        if enrich and len(raw_positions) < 100:
            _now_w = time.time()
            _need = []
            for _p in raw_positions:
                _tk = _p.get("market_id") or _p.get("ticker", "")
                _c = _market_cache.get(_tk) if _tk else None
                if _tk and not (_c and (_now_w - _c["ts"]) < _MARKET_CACHE_TTL):
                    _need.append(_tk)
            for _i in range(0, len(_need), 100):
                try:
                    _d = kalshi_get("/markets", {"tickers": ",".join(_need[_i:_i + 100]), "limit": 100})
                    for _m in _d.get("markets", []):
                        _mt = _m.get("ticker", "")
                        if _mt:
                            _market_cache[_mt] = {"data": _m, "ts": time.time()}
                except Exception as e:
                    print(f"[portfolio] batch market warm failed: {e}")

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

            # Hide positions sold in the last RECENTLY_SOLD_TTL seconds — Kalshi's
            # positions API lags a few seconds after a sale, so without this a
            # just-sold position reappears (and can be sold again) on refresh.
            if _is_recently_sold(ticker, "yes" if qty > 0 else "no"):
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
            # When 1000+ positions + enrich=true, skip expensive market lookups (they race
            # with the scan loop for rate-limit lock). Only use cached balance/positions.
            # Frontend will request individual position enrichment on-demand.
            if enrich and len(raw_positions) < 100:
                time.sleep(0.01)
                try:
                    mkt          = _get_market(ticker)
                    event_ticker = mkt.get("event_ticker", "")
                    market_title = _pretty_title(ticker, _event_title(event_ticker) or mkt.get("title", ticker))
                    category     = mkt.get("category", "")
                    current_yes  = _mark_price_cents(mkt, "yes")
                    current_no   = _mark_price_cents(mkt, "no")
                    close_time   = mkt.get("close_time") or mkt.get("expiration_time")
                except Exception:
                    pass
            else:
                # Fast mode: make NO new market call, but REUSE cached market data if
                # the scan loop already fetched it (it scans these same crypto markets
                # constantly). Instant live prices with zero extra API hits — so the
                # CURRENT/PROFIT columns show numbers immediately instead of dashes.
                _c = _market_cache.get(ticker)
                if _c and (time.time() - _c["ts"]) < _MARKET_CACHE_TTL:
                    _m = _c["data"]
                    event_ticker = _m.get("event_ticker", "")
                    market_title = _pretty_title(ticker, _event_title(event_ticker) or _m.get("title", ticker))
                    category     = _m.get("category", "")
                    current_yes  = _mark_price_cents(_m, "yes")
                    current_no   = _mark_price_cents(_m, "no")
                    close_time   = _m.get("close_time") or _m.get("expiration_time")
                else:
                    event_ticker = ""
                    market_title = _title_cache.get(ticker) or _humanize_ticker(ticker)
                    category = ""
                    current_yes = None
                    current_no = None
                    close_time = None

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
                "bought_at":      (bot_info.get("bought_at") if bot_info else None) or _buy_ts_by_ticker.get(ticker) or p.get("created_time"),
                "status":         bot_info.get("status", "open") if bot_info else "open",
                "profile":        bot_info.get("profile") if bot_info else None,
            })
    except Exception as e:
        positions_ok = False
        print(f"[portfolio] positions error: {e}")

    # ── Tracked fallback ──────────────────────────────────────────────────────
    # Merge in any bot-tracked "open" positions not returned by Kalshi's live API.
    # This happens right after a buy (Kalshi delay) or when the positions API errors.
    live_tickers = {p["ticker"] for p in positions}
    with _lock:
        tracked_snap = {k: dict(v) for k, v in tracked.items()}

    for ticker, info in tracked_snap.items():
        if ticker in live_tickers or info.get("status") not in ("open", "selling"):
            continue
        # Also skip anything just sold via the UI (status may not have flushed yet)
        if _is_recently_sold(ticker, info.get("side", "yes")):
            continue

        # Skip if the market has expired (close_time is in the past)
        # This prevents stale fallback positions from appearing hours after expiry
        bought_at = info.get("bought_at")
        if bought_at:
            try:
                bought_dt = datetime.fromisoformat(bought_at.replace("Z", "+00:00"))
                # If bought 30+ minutes ago and we still don't have live Kalshi data,
                # it's probably expired — don't serve it as fallback
                age_mins = (datetime.now(timezone.utc) - bought_dt).total_seconds() / 60
                if age_mins > 30:
                    # Market should have settled by now. If it's not in live Kalshi data,
                    # mark it sold and skip it. Clear settlements cache so next Summary
                    # fetch pulls fresh data from Kalshi.
                    with _lock:
                        if ticker in tracked and tracked[ticker].get("status") in ("open", "selling"):
                            tracked[ticker]["status"] = "sold"
                            tracked[ticker].setdefault("sold_by", "expired")
                            tracked[ticker].setdefault("sold_at", datetime.now(timezone.utc).isoformat())
                    # Invalidate settlements cache so Summary pulls fresh data from Kalshi
                    global _settlements_cache
                    _settlements_cache.clear()
                    _save_tracked()
                    continue
            except Exception:
                pass

        # Ghost reconciliation: if the live positions fetch SUCCEEDED AND returned a
        # real (non-empty) position list, but this tracked-"open" position isn't in
        # it, it's been sold/closed externally — mark it sold so it stops reappearing
        # (unless bought in the last 90s: Kalshi post-buy propagation grace).
        # CRITICAL: only do this when raw_positions is NON-EMPTY. An empty result is
        # almost always the bot starving the API (transient), NOT "everything sold" —
        # marking all positions sold on a transient empty wipes the whole portfolio.
        if positions_ok and raw_positions:
            age = _position_age_seconds(info)
            if age is None or age > 90:
                with _lock:
                    if ticker in tracked and tracked[ticker].get("status") in ("open", "selling"):
                        tracked[ticker]["status"] = "sold"
                        tracked[ticker].setdefault("sold_by", "external")
                        tracked[ticker].setdefault("sold_at", datetime.now(timezone.utc).isoformat())
                _save_tracked()
                continue
        event_ticker = ""
        # Prefer the remembered pretty title over the tracked title or raw ticker.
        market_title = _title_cache.get(ticker) or info.get("title") or _humanize_ticker(ticker)
        category     = ""
        current_yes  = None
        current_no   = None
        close_time   = None

        # Reuse cached market prices (from the scan loop) with no new API call, so
        # tracked positions also show live numbers instantly instead of dashes.
        _cf = _market_cache.get(ticker)
        if _cf and (time.time() - _cf["ts"]) < _MARKET_CACHE_TTL:
            _mf = _cf["data"]
            event_ticker = _mf.get("event_ticker", "")
            market_title = _pretty_title(ticker, _event_title(event_ticker) or _mf.get("title", market_title))
            category     = _mf.get("category", "")
            current_yes  = _mark_price_cents(_mf, "yes")
            current_no   = _mark_price_cents(_mf, "no")
            close_time   = _mf.get("close_time") or _mf.get("expiration_time")

        # Only make NEW market calls if enriching AND < 100 total positions
        # (to avoid rate-limit starvation during heavy portfolios)
        if enrich and len(raw_positions) < 100:
            try:
                mkt        = _get_market(ticker)
                mkt_status = (mkt.get("status") or "").lower()
                if mkt_status in ("settled", "resolved", "finalized", "closed"):
                    with _lock:
                        if ticker in tracked and tracked[ticker].get("status") in ("open", "selling"):
                            tracked[ticker]["status"] = "sold"
                    _save_tracked()
                    continue
                event_ticker = mkt.get("event_ticker", "")
                market_title = _pretty_title(ticker, _event_title(event_ticker) or mkt.get("title", ticker))
                category     = mkt.get("category", "")
                current_yes  = _mark_price_cents(mkt, "yes")
                current_no   = _mark_price_cents(mkt, "no")
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
            "close_time":     close_time,
            "bot_bought":     True,
            "buy_price":      info.get("buy_price"),
            "strategy":       info.get("strategy"),
            "target_pct":     info.get("target_pct"),
            "bought_at":      info.get("bought_at") or _buy_ts_by_ticker.get(ticker),
            "status":         info.get("status", "open"),
            "profile":        info.get("profile"),
        })
        pass  # suppress repeated fallback log spam

    # (api_positions_value — Kalshi's own open-positions value — was computed up
    # front, before the positions fetch, so the fetch could retry on a starved empty.)

    # Cache the last good positions list (in memory AND on disk). If THIS fetch came
    # back empty but the balance API still shows open positions, the positions fetch
    # was just starved by the bot's API traffic — serve the cached list instead of
    # showing nothing. Persisting to disk means an Update/restart no longer wipes the
    # fallback, so the user never sees a blank "No open positions" after a restart.
    global _last_positions_cache
    if positions:
        _last_positions_cache = {"data": positions, "value": round(portfolio_value, 2), "ts": time.time()}
        _save_positions_cache()
    else:
        # Live fetch empty — try in-memory cache, then the disk cache (survives restart).
        if not _last_positions_cache.get("data"):
            _load_positions_cache()
        if api_positions_value > 0 and _last_positions_cache.get("data"):
            positions = _last_positions_cache["data"]
            positions_ok = False  # tell the frontend this is cached, not a fresh empty
            if portfolio_value == 0.0:
                portfolio_value = _last_positions_cache.get("value") or api_positions_value

    if portfolio_value == 0.0:
        portfolio_value = api_positions_value

    total_value = total_account if total_account is not None else round((balance or 0) + portfolio_value, 2)

    # Settlements: on enrich, refresh (cache-or-fetch). On the FAST path, still
    # return FRESH cached settlements instantly so they show without waiting on the
    # slow enrich — but never trigger a slow fetch on the fast path.
    settle_hours = int(request.args.get("settlement_hours", 24))
    recent_settlements = []
    if enrich:
        recent_settlements = _cached_settlements(hours=settle_hours)
    else:
        _c = _settlements_cache.get(settle_hours)
        if _c:
            recent_settlements = _c["data"]

    live = bool(_balance_cache.get("live", True))
    return jsonify({
        "balance":            balance,           # spendable cash
        "positions_value":    round(portfolio_value, 2),  # open positions value only
        "portfolio_value":    total_value,       # total account = cash + positions (matches Kalshi)
        "positions":          positions,
        "positions_ok":       positions_ok,      # did the live Kalshi positions fetch succeed?
        "balance_live":       live,              # whether balance is fresh (true) or stale/failed (false)
        "recent_settlements": recent_settlements,
    })


@app.route("/api/settlements")
def api_settlements():
    """Dedicated settlements endpoint, decoupled from /api/portfolio so the Recent
    Settlements table loads on its own and isn't blocked by slow position enrichment.
    First call may take a few seconds (Kalshi), then it's cached and instant."""
    try:
        hours = int(request.args.get("hours", 24))
    except (TypeError, ValueError):
        hours = 24
    try:
        data = _cached_settlements(hours=hours)
        return jsonify({"settlements": data, "ok": True})
    except Exception as e:
        print(f"[settlements] error: {e}")
        # Serve whatever's cached even if a refresh failed, so the table isn't blank.
        _c = _settlements_cache.get(hours)
        return jsonify({"settlements": (_c["data"] if _c else []), "ok": False, "error": str(e)})


@app.route("/api/debug-settle")
def api_debug_settle():
    """Return raw first page of Kalshi /portfolio/settlements for debugging field names."""
    raw = kalshi_get("/portfolio/settlements", {"limit": 5})
    batch = raw.get("settlements", [])
    result = []
    for s in batch:
        yes_cnt, no_cnt, yc, nc = _settle_counts(s)
        result.append({
            "keys": list(s.keys()),
            "raw": {k: s[k] for k in s},
            "parsed": {"yes_cnt": yes_cnt, "no_cnt": no_cnt, "yes_cost": yc, "no_cost": nc,
                       "revenue": s.get("revenue"), "market_result": s.get("market_result")}
        })
    return jsonify({"count": len(batch), "settlements": result})


def _recent_settlements(hours: int = 24) -> list:
    """Fetch Kalshi settlements from the past `hours` hours, enriched with title/category."""
    cutoff_ts = datetime.now(timezone.utc) - timedelta(hours=hours)
    out = []
    cursor = None
    pages = 0
    # Reset the self-diagnostic for this run
    _settle_debug.update({"raw": 0, "in_window": 0, "skipped_empty": 0,
                          "skipped_hedged": 0, "kept": 0, "last_error": "",
                          "sample_keys": [], "ran_at": time.time()})
    try:
        while pages < 10:
            params = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            data = kalshi_get("/portfolio/settlements", params)
            batch = data.get("settlements", [])
            _settle_debug["raw"] += len(batch)
            if pages == 0:
                print(f"[settle] /portfolio/settlements returned {len(batch)} items (cutoff={cutoff_ts.isoformat()[:19]})")
                if batch:
                    s0 = batch[0]
                    _settle_debug["sample_keys"] = list(s0.keys())
                    print(f"[settle] first item keys: {list(s0.keys())}")
                    print(f"[settle] first item: { {k: s0[k] for k in list(s0.keys())[:12]} }")
            if not batch:
                break
            stop = False
            for s in batch:
                ts_str = s.get("settled_time", "") or s.get("settlement_time", "") or s.get("created_time", "")
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if ts < cutoff_ts:
                    stop = True
                    break
                _settle_debug["in_window"] += 1

                ticker = s.get("ticker", "")
                evt    = s.get("event_ticker", "")
                # CHEAP title only — NO per-settlement API calls. _event_title and
                # _get_market each hit Kalshi (uncached = one call EACH, per settlement).
                # For 70+ settlements through a rate limiter the bot is already saturating,
                # that made the whole fetch take minutes — and since the cache is only
                # stored when the fetch FINISHES, the Summary showed 0 settled rows the
                # entire time. _pretty_title uses the on-disk title cache (the bot already
                # cached these titles when it BOUGHT them) or humanizes the ticker. Instant.
                title    = _pretty_title(ticker, "")
                category = ""

                yes_cnt, no_cnt, yes_cost, no_cost = _settle_counts(s)
                revenue  = float(s.get("revenue") or 0) / 100   # cents → dollars
                result   = s.get("market_result", "")
                # Debug: log first settlement we encounter so we can see actual API field names
                if not getattr(_recent_settlements, "_logged_sample", False):
                    print(f"[settle-debug] sample keys for {ticker}: {list(s.keys())}")
                    print(f"[settle-debug] yes_cnt={yes_cnt} no_cnt={no_cnt} yes_cost={yes_cost} no_cost={no_cost} revenue={revenue} result={result}")
                    _recent_settlements._logged_sample = True

                # Skip entries with no position data AND no market result (truly empty placeholders)
                has_result = bool(result)  # market_result = "yes"/"no" means it's a real settlement
                if not has_result and yes_cnt < 0.001 and no_cnt < 0.001 and revenue < 0.001:
                    _settle_debug["skipped_empty"] += 1
                    continue

                # Skip hedged positions (bought both sides → net zero, closed pre-settlement).
                # Kalshi records these as settlements with revenue=0; the realized PnL was
                # taken when the offsetting trade was made, not at settlement.
                if yes_cnt > 0.001 and no_cnt > 0.001:
                    _settle_debug["skipped_hedged"] += 1
                    continue

                if yes_cnt > 0.001:
                    side, count, cost = "yes", yes_cnt, yes_cost
                elif no_cnt > 0.001:
                    side, count, cost = "no", no_cnt, no_cost
                else:
                    # Counts still unknown — infer side from market_result + revenue
                    # (losing side gets 0 revenue; winning side gets paid out)
                    side = result if result in ("yes", "no") else "yes"
                    count = 0  # unknown; display will show "?"
                    cost  = 0

                # Real Kalshi fee: ceil(0.07 × C × P × (1−P)) charged at the BUY,
                # win or lose — the old flat "1¢/contract on wins only" both
                # undercounted losses and overcounted big wins.
                avg_price_cents = (cost / count * 100) if count > 0.001 else 0
                fee = _kalshi_fee(count, avg_price_cents)

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
                    "profile":      (tracked.get(ticker) or {}).get("profile"),
                })
                _settle_debug["kept"] += 1
            if stop:
                break
            cursor = data.get("cursor")
            if not cursor:
                break
            pages += 1
    except Exception as e:
        _settle_debug["last_error"] = f"{type(e).__name__}: {e}"
        print(f"[settlements] error: {e}")
        import traceback
        traceback.print_exc()

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
        # Sold early = TWO taker fees (one on buy, one on sell)
        fee   = _kalshi_fee(count, bp) + (_kalshi_fee(count, sp) if sp else 0)
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
            "profile":      pos.get("profile"),
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

    # Hide multi-outcome: skip markets that are part of a price-level series
    # Patterns: KXBTC-date-B73500 (point price), KXBTCD-date-T73999.99 (range)
    if hide_multi:
        import re as _re
        if _re.search(r'-[BT]\d[\d.]+$', tkr):  # ends with -B73500 or -T73999.99
            return None
        if tkr.upper().startswith("KXBTCD") or tkr.upper().startswith("KXETHD") or \
           tkr.upper().startswith("KXSOLD") or tkr.upper().startswith("KXXRPD"):
            return None  # daily/range crypto series

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
        minutes        = float(request.args.get("minutes", 15))   # fractional ok (0.25–5 custom)
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
        crypto_times_raw = request.args.get("crypto_times", "")
        # "none"/empty = Crypto enabled but no time-type sub-filter selected → allow ALL crypto types
        # (previously this became {"none"} which matched no real market type and blocked all crypto)
        if (not crypto_times_raw) or crypto_times_raw.strip().lower() == "none":
            crypto_times = None
        else:
            crypto_times = set(t.strip() for t in crypto_times_raw.split(",") if t.strip())
        hide_multi = request.args.get("hide_multi", "false").lower() == "true"

        # Clamp minutes: lower bound 0.25 (mirrors the bot's _posfloat) so a 0 or
        # negative value can't make cutoff land in the past and silently return
        # nothing; upper bound 525600 (~1yr) to prevent timedelta overflow.
        minutes = max(0.25, min(minutes, 525600))

        # Convert "show" logic to "exclude" logic for the filter function
        no_crypto    = not show_crypto
        no_combo     = not show_combo
        no_sports    = not show_sports
        no_politics  = not show_politics
        no_economics = not show_economics
    except ValueError:
        return jsonify({"error": "Invalid parameters"}), 400

    now    = datetime.now(timezone.utc)
    try:
        cutoff = now + timedelta(minutes=minutes)
    except OverflowError:
        cutoff = now + timedelta(days=365)  # fallback cap
    results = []

    # NOTE: Sell strategy should NOT interfere with buy scan range.
    # Buy range (min_thr/max_thr) is independent of sell logic.
    # If user sets conflicting values (e.g., buy range 20-96% but sell at 15¢),
    # that's their choice — we don't cap one based on the other.

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
        # Crypto — 15-minute timed markets (rotate every 15 min; must probe directly)
        "KXBTC15M", "KXETH15M", "KXSOL15M", "KXHYPE15M",
        "KXDOGE15M", "KXBNB15M", "KXXRP15M",
        # Crypto — 30-minute / 1-hour timed
        "KXBTC30M", "KXETH30M", "KXSOL30M",
        "KXBTC1H", "KXETH1H", "KXSOL1H",
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
            # Track which token is currently being scanned (for UI highlighting)
            token = _token_from_series(st)
            if token and show_crypto:
                _scan_state["T1" if "T1" in active_profiles else "T2"]["token"] = token
                _scan_state["T1" if "T1" in active_profiles else "T2"]["timestamp"] = time.time()

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
    # (This also serves as idempotency: if a buy succeeded but response timed out,
    # a retry will be rejected here because the position already exists)
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

    # Guard against division by zero (corrupted market data)
    if price_c <= 0 or price_c > 99:
        return jsonify({"error": f"Invalid market price {price_c}¢ — market may be closed or corrupted"}), 400


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
        # Log the FULL Kalshi rejection (status + body) so the 502 cause is visible.
        # The client only gets a truncated toast, so without this the real reason
        # (price moved, insufficient balance, market closed, etc.) is lost.
        err_text = e.response.text[:500]
        print(f"[buy] ORDER FAILED {e.response.status_code} {ticker} {side} x{contracts} @{price_c}c: {err_text}")
        try:
            with open(HERE / "buy_errors.log", "a", encoding="utf-8") as f:
                f.write(f"{datetime.now(timezone.utc).isoformat()} {e.response.status_code} {ticker} {side} count={contracts} price={price_c} body={err_text}\n")
        except Exception:
            pass
        # Market closed/expired between scan and order — normal for short-expiry
        # markets, and nothing was bought. Clean, non-alarming message + flag.
        if e.response.status_code == 404 or "market_not_found" in err_text:
            return jsonify({
                "error": f"{ticker} closed before the order went through — skipped (normal for fast-expiring markets, nothing was bought)",
                "market_closed": True,
            }), 409
        return jsonify({"error": f"Order failed ({e.response.status_code}): {e.response.text[:200]}"}), 502
    except Exception as e:
        print(f"[buy] EXCEPTION {ticker}: {type(e).__name__}: {e}")
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
        # Re-buying a ticker clears any recently-sold hide so the new position
        # shows immediately instead of being suppressed by the 120s sold window.
        _recently_sold.pop(ticker, None)
    _save_tracked()
    _balance_cache["ts"] = 0  # cash changed — force a fresh balance on next read

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

    # Safely convert count to float (supports fractional quantities like 0.08, 0.91)
    try:
        count = float(data.get("count", 0))
    except (ValueError, TypeError):
        return jsonify({"error": f"Invalid count: {data.get('count')}"}), 400

    if not ticker:
        return jsonify({"error": "Missing ticker"}), 400
    if side not in ("yes", "no"):
        return jsonify({"error": f"Invalid side '{side}' (must be 'yes' or 'no')"}), 400
    if count < 0.001 or count > 1e6:
        return jsonify({"error": f"Invalid count {count} (must be 0.001-1,000,000)"}), 400

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

        # Kalshi API only accepts whole numbers - sell what we can, leave fractional for Kalshi
        count_int = int(count)  # Floor: 1.53 → 1, 0.53 → 0

        if count_int < 1:
            # Less than 1 contract - can't sell through Kalshi API
            print(f"[sell] {ticker}: {count} contracts (fractional < 1, error)")
            return jsonify({"error": f"Can't sell {count} contracts — less than 1. Position will resolve at expiry."}), 400

        # LIMIT SELL at current bid price — locked in price, won't sell below.
        # Safer than market orders which could fill at terrible prices if liquidity dries up.
        order_payload = {
            "ticker": ticker,
            "client_order_id": str(uuid.uuid4()),  # Required by Kalshi API
            "action": "sell",
            "side":   side,
            "type":   "limit",  # LIMIT: won't sell below this price
            "count":  count_int,
        }
        price_key = "yes_price" if side == "yes" else "no_price"
        order_payload[price_key] = bid_cents  # sell at this price or better

        print(f"[sell] {ticker} {side} × {count_int} LIMIT @ {bid_cents}¢")
        result = kalshi_post("/portfolio/orders", order_payload)
        order_status = result.get("order", {}).get("status", "")

        # "canceled" = limit order didn't fill (no buyers at that price).
        # Return error and let frontend offer retry options (lower price, wait, etc).
        if order_status == "canceled":
            print(f"[sell] {ticker} LIMIT canceled — no buyers at {bid_cents}¢")
            return jsonify({
                "error": f"No buyers at {bid_cents}¢",
                "reason": "limit_no_fill",
                "tried_price": bid_cents,
                "suggest_lower": max(1, bid_cents - 5),  # suggest ±5¢ lower as fallback
            }), 400
    except req.HTTPError as e:
        err_text = e.response.text[:500]
        print(f"[sell] HTTPError {e.response.status_code}: {err_text}")
        # Log sell errors to file for debugging
        try:
            with open(HERE / "buy_errors.log", "a", encoding="utf-8") as f:
                f.write(f"{datetime.now(timezone.utc).isoformat()} SELL {e.response.status_code} {ticker} {side} count={count_int} body={err_text}\n")
        except Exception:
            pass
        # Market already resolved/closed — common on sells of expiring positions.
        if e.response.status_code == 404 or "market_not_found" in err_text:
            return jsonify({
                "error": f"{ticker} already resolved/closed — can't sell (it will settle on its own)",
                "market_closed": True,
            }), 409
        return jsonify({"error": f"Sell failed ({e.response.status_code}): {e.response.text[:200]}"}), 502
    except Exception as e:
        print(f"[sell] Exception: {type(e).__name__}: {e}")
        return jsonify({"error": str(e)}), 500

    with _lock:
        if ticker in tracked:
            # Only mark as "sold" if the order actually filled immediately.
            # For LIMIT orders, "pending" means it's waiting for buyers — don't hide it yet.
            if order_status in ("filled", "accepted"):
                tracked[ticker]["status"]  = "sold"
                tracked[ticker]["sold_by"] = "human"  # manually sold via UI
            else:
                # LIMIT order is pending (waiting for match). Keep position visible.
                tracked[ticker]["status"] = "open"
        # Record the sale for EVERY position (tracked or not) so the portfolio
        # endpoint hides it during Kalshi's settlement-propagation delay. This is
        # what stops non-bot positions from reappearing after a hard refresh.
        _recently_sold[ticker] = {"side": side, "at": time.time()}
    _save_tracked()
    _balance_cache["ts"] = 0  # cash changed — force a fresh balance on next read

    order = result.get("order", result)
    # Return executed price (if available) so frontend can calculate accurate profit
    executed_price = order.get("price")  # Kalshi returns executed price in order response

    # Distinguish between filled (completed) and pending (waiting for buyers)
    is_filled = order_status in ("filled", "accepted")

    # Record filled manual sells in the activity log (with profit if we know cost basis).
    if is_filled:
        exec_c = executed_price if executed_price else bid_cents
        _pos = tracked.get(ticker, {})
        _bp = _pos.get("buy_price")
        _profit = round(count_int * (exec_c - _bp) / 100, 2) if _bp else None
        _record_activity("sell", ticker=ticker, side=side, count=count_int,
                         price=exec_c, profit=_profit, sold_by="human",
                         title=_pos.get("title", ticker), category=_pos.get("category", ""))

    return jsonify({
        "ok": True,
        "order_id": order.get("order_id"),
        "order_status": order_status,
        "is_filled": is_filled,  # True = order filled, False = pending/waiting
        "executed_price_cents": executed_price if executed_price else bid_cents,
        "bid_price_cents": bid_cents,
        "count": count_int
    })


@app.route("/api/positions")
def positions():
    # Refresh current prices for open positions
    with _lock:
        snap = {k: dict(v) for k, v in tracked.items()}

    for ticker, pos in snap.items():
        if pos.get("status") not in ("open", "selling"):
            continue
        try:
            m     = _get_market(ticker)
            bid_d = m.get("yes_bid_dollars") if pos["side"] == "yes" else m.get("no_bid_dollars")
            bid   = _dollars_to_cents(bid_d)
            if bid is not None and pos.get("buy_price") and pos["buy_price"] > 0:
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
    """Return realized P&L from Kalshi settlements (excludes deposits).
    P&L calculated from actual settled positions, not portfolio snapshots."""
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

    # Fetch settlements from Kalshi — only these count as real profit (excludes deposits)
    settle_by_time = {}  # ts -> pnl for that settlement
    try:
        cursor = None
        pages = 0
        while pages < 20:
            params = {"limit": 100}
            if cursor: params["cursor"] = cursor
            data = kalshi_get("/portfolio/settlements", params)
            batch = data.get("settlements", [])
            if not batch: break
            for s in batch:
                ts_str = s.get("settled_time", "")
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    ts_float = ts.timestamp()
                except ValueError:
                    continue
                yes_cnt, no_cnt, yes_cost, no_cost = _settle_counts(s)
                rev = float(s.get("revenue") or 0) / 100
                cost = yes_cost if yes_cnt > 0.001 else no_cost
                cnt = yes_cnt if yes_cnt > 0.001 else no_cnt
                fee = _kalshi_fee(cnt, (cost / cnt * 100) if cnt > 0.001 else 0)
                pnl = rev - cost - fee
                settle_by_time[ts_float] = pnl
            cursor = data.get("cursor")
            if not cursor: break
            pages += 1
    except Exception as e:
        print(f"[pnl_history] settlements error: {e}")

    # Sort settlements by timestamp
    sorted_settles = sorted(settle_by_time.items())

    # Calculate cumulative P&L at each time window
    past_values = {}
    for label, secs in periods:
        cutoff = now - secs
        cumul_pnl = 0.0
        for ts, pnl in sorted_settles:
            if ts >= cutoff:
                cumul_pnl += pnl
        past_values[label] = round(cumul_pnl, 2) if cumul_pnl != 0 else None

    # Also calculate total realized P&L (all time)
    total_realized_pnl = round(sum(pnl for pnl in settle_by_time.values()), 2)

    return jsonify({
        "past_values":        past_values,
        "total_realized_pnl": total_realized_pnl,
        "settlements_count":  len(settle_by_time),
    })


@app.route("/api/strategy", methods=["GET", "POST"])
def set_strategy():
    global sell_strategy
    # Which profile's SELL strategy? Default = T1 (the front-page bot). The bot reads
    # each active profile's own sell strategy directly; the global `sell_strategy` is a
    # T1 mirror used only as a fallback by the monitor.
    req_profile = (request.args.get("profile")
                   or (request.get_json(silent=True) or {}).get("profile"))
    target = req_profile if req_profile in PROFILE_IDS else "T1"

    if request.method == "GET":
        return jsonify(_sell_profiles.get(target, sell_strategy))

    data = request.get_json(silent=True) or {}
    mode = data.get("mode", "resolution")

    # Frontend sends actual mode: "resolution", "profit", "profit_dollars", "target_price"
    if mode not in ("resolution", "profit", "profit_dollars", "target_price"):
        return jsonify({"error": "mode must be resolution, profit, profit_dollars, or target_price"}), 400

    try:
        pct  = float(data.get("target_pct", 10)) if data.get("target_pct") is not None else None
        dol  = float(data.get("target_dollars")) if data.get("target_dollars") is not None else None
        tp   = float(data.get("target_price_cents")) if data.get("target_price_cents") is not None else None
        bip  = float(data.get("buy_in_price_cents")) if data.get("buy_in_price_cents") is not None else None
        slp  = float(data.get("stop_loss_pct")) if data.get("stop_loss_pct") is not None else None

        # Validate numeric ranges to prevent nonsensical values
        if pct is not None and (pct < 1 or pct > 999):
            return jsonify({"error": "target_pct must be 1-999"}), 400
        if dol is not None and (dol < 0.01 or dol > 1000):
            return jsonify({"error": "target_dollars must be 0.01-1000"}), 400
        if tp is not None and (tp < 1 or tp > 99):
            return jsonify({"error": "target_price_cents must be 1-99"}), 400
        if bip is not None and (bip < 1 or bip > 99):
            return jsonify({"error": "buy_in_price_cents must be 1-99"}), 400
        if slp is not None and (slp < 1 or slp > 99):
            return jsonify({"error": "stop_loss_pct must be 1-99"}), 400
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid numeric values"}), 400

    # Edit a working copy of the target profile's sell strategy.
    strat = dict(_sell_profiles.get(target, _DEFAULT_SELL_STRATEGY))
    strat["mode"] = mode
    if pct is not None: strat["target_pct"] = pct
    if dol is not None: strat["target_dollars"] = dol
    if tp is not None: strat["target_price_cents"] = tp
    if bip is not None: strat["buy_in_price_cents"] = bip
    # Stop-loss: sending the key with null clears it (checkbox off).
    if "stop_loss_pct" in data:
        strat["stop_loss_pct"] = slp

    _sell_profiles[target] = strat
    if target == "T1":
        sell_strategy = strat   # keep the legacy T1 mirror current
        try: STRATEGY_FILE.write_text(json.dumps(sell_strategy), encoding="utf-8")
        except Exception: pass
    _save_profiles()
    return jsonify({"ok": True, "profile": target, "active": active_profiles, "strategy": strat})


@app.route("/api/saved-strategies", methods=["GET"])
def get_saved_strategies():
    """Get saved strategies for a profile (T1=Scanner, T2=Lotto).
    Returns exactly 10 slots per profile."""
    profile = request.args.get("profile", "T1")
    try:
        if SAVED_STRATS_FILE.exists():
            all_data = json.loads(SAVED_STRATS_FILE.read_text(encoding="utf-8"))
            if isinstance(all_data, dict):  # Per-profile format
                data = all_data.get(profile, [])
            else:  # Legacy flat format (assume T1)
                data = all_data if profile == "T1" else []
        else:
            data = []
    except Exception:
        data = []
    # Always return exactly 10 slots
    slots = (data + [None] * 10)[:10]
    return jsonify({"slots": slots, "profile": profile})


@app.route("/api/saved-strategies", methods=["POST"])
def set_saved_strategies():
    """Save strategies for a profile (T1=Scanner, T2=Lotto)."""
    data = request.get_json(silent=True) or {}
    profile = data.get("profile", "T1")
    slots = data.get("slots", [])
    try:
        # Load existing data
        if SAVED_STRATS_FILE.exists():
            try:
                all_data = json.loads(SAVED_STRATS_FILE.read_text(encoding="utf-8"))
                if not isinstance(all_data, dict):  # Migrate legacy format
                    all_data = {"T1": all_data}
            except:
                all_data = {}
        else:
            all_data = {}

        # Update the profile's slots
        all_data[profile] = slots

        # Save back
        SAVED_STRATS_FILE.write_text(json.dumps(all_data, indent=2), encoding="utf-8")
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True, "profile": profile})


@app.route("/api/sell-settings", methods=["POST"])
def set_sell_settings():
    global sell_settings
    data = request.get_json(silent=True) or {}
    if "skip_auto_sell_near_resolution" in data:
        sell_settings["skip_auto_sell_near_resolution"] = bool(data["skip_auto_sell_near_resolution"])
    if "skip_auto_sell_minutes" in data:
        sell_settings["skip_auto_sell_minutes"] = max(1, int(data["skip_auto_sell_minutes"]))
    # NOTE: stop-loss is NOT handled here. It lives in sell_strategy (set via
    # /api/strategy) because that's the dict the monitor reads. Writing it here
    # was the original bug — it silently never fired.
    return jsonify({"ok": True, "settings": sell_settings})


@app.route("/api/buy-settings", methods=["GET", "POST"])
def buy_settings_endpoint():
    """GET returns the auto-bot's live BUY filters; POST updates them.

    This is the bridge that makes the headless bot trade what the UI shows. The
    frontend posts here whenever buy filters change; `_bot_thread` reads the saved
    values live every cycle. Unknown keys are ignored; types are coerced safely.
    """
    global buy_settings
    # Which profile's settings are we reading/writing? Default = T1 (front page). The
    # bot reads each active profile's settings directly from _profiles.
    req_profile = (request.args.get("profile")
                   or (request.get_json(silent=True) or {}).get("profile"))
    target = req_profile if req_profile in PROFILE_IDS else "T1"

    if request.method == "GET":
        return jsonify(_profiles.get(target, buy_settings))

    data = request.get_json(silent=True) or {}
    work = dict(_profiles.get(target, _DEFAULT_BUY_SETTINGS))
    _apply_buy_edits(work, data)
    _profiles[target] = work
    if target == "T1":
        buy_settings = work          # keep the legacy T1 mirror current
        _save_buy_settings()         # writes T1 mirror + profiles to disk
    else:
        _save_profiles()
    return jsonify({"ok": True, "profile": target, "active": active_profiles, "settings": work})


@app.route("/api/active-profile", methods=["GET", "POST"])
def active_profile_endpoint():
    """GET → list of currently-active bots. POST to change which bots run:
      {"profile":"T2","on":true}   → toggle a single bot on/off
      {"profiles":["T1","T2"]}     → set the whole active set
    MULTIPLE bots can run at once (e.g. the regular bot + the Lotto bot). Each trades
    with its own profile's settings; they share the rate limiter so total API/order
    traffic stays within Kalshi's limits."""
    global active_profiles
    if request.method == "GET":
        return jsonify({"active": active_profiles, "profiles": PROFILE_IDS})
    data = request.get_json(silent=True) or {}

    if isinstance(data.get("profiles"), list):
        new = [p for p in data["profiles"] if p in PROFILE_IDS]
    else:
        p = data.get("profile")
        if p not in PROFILE_IDS:
            return jsonify({"ok": False, "error": "bad profile"}), 400
        new = list(active_profiles)
        on = bool(data.get("on", p not in new))  # default = toggle
        if on and p not in new:
            new.append(p)
        elif not on and p in new:
            new.remove(p)

    # Keep a stable order (T1, T2, …) and de-dup.
    active_profiles = [pid for pid in PROFILE_IDS if pid in new]
    _save_profiles()
    return jsonify({"ok": True, "active": active_profiles})


@app.route("/api/recent-buys")
def get_recent_buys():
    """Fetch and clear the queue of bot-made buys that the frontend hasn't announced yet."""
    global _recent_buys_queue
    buys = _recent_buys_queue[:]  # copy the list
    _recent_buys_queue = []        # clear the queue
    return jsonify({"buys": buys})


@app.route("/api/api-key-status")
def api_api_key_status():
    """Return a masked version of the current API key ID so the UI can confirm credentials are loaded."""
    try:
        key_file = _find_cred_file(_API_KEY_NAMES)
        if key_file and key_file.exists():
            raw = key_file.read_text(encoding="utf-8").strip()
            # Mask middle — show first 8 and last 4 chars
            masked = raw[:8] + "…" + raw[-4:] if len(raw) > 12 else raw[:4] + "…"
            return jsonify({"ok": True, "key_id": masked})
        return jsonify({"ok": True, "key_id": None})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/update-api-key", methods=["POST"])
def api_update_api_key():
    """Write new API key ID and private key PEM to disk, then hot-reload credentials."""
    global API_KEY, PRIVATE_KEY
    data = request.get_json(silent=True) or {}
    key_id  = (data.get("key_id") or "").strip()
    priv_pem = (data.get("private_key") or "").strip()

    if not key_id or not priv_pem:
        return jsonify({"ok": False, "error": "Both key_id and private_key are required"})
    if "BEGIN" not in priv_pem:
        return jsonify({"ok": False, "error": "private_key must be PEM format (-----BEGIN…-----)"})

    CREDS_DIR.mkdir(parents=True, exist_ok=True)
    key_path  = CREDS_DIR / _API_KEY_NAMES[0]
    priv_path = CREDS_DIR / _PRIV_KEY_NAMES[0]

    try:
        key_path.write_text(key_id, encoding="utf-8")
        priv_path.write_bytes(priv_pem.encode())
        # Hot-reload
        API_KEY, PRIVATE_KEY, _, _ = _load_creds()
        _log("[api-key] credentials updated and reloaded via web UI")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


REGISTRATIONS_FILE = HERE / "registrations.jsonl"
REGISTRATION_WEBHOOK = os.getenv("KALSHIBOT_REG_WEBHOOK", "")  # optional: set to receive signups

@app.route("/api/register-user", methods=["POST"])
def api_register_user():
    """Store optional user registration data locally and forward to dev webhook."""
    data = request.get_json(silent=True) or {}
    entry = {
        "ts": time.time(),
        "date": datetime.now(timezone.utc).isoformat(),
        "nickname": str(data.get("nickname", ""))[:64],
        "email": str(data.get("email", ""))[:128],
        "state": str(data.get("state", ""))[:64],
        "country": str(data.get("country", ""))[:64],
        "version": BOT_VERSION,
    }
    try:
        with open(REGISTRATIONS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        _log(f"[register] local write failed: {e}")

    if REGISTRATION_WEBHOOK:
        try:
            req.post(REGISTRATION_WEBHOOK, json=entry, timeout=5)
        except Exception:
            pass

    return jsonify({"ok": True})


@app.route("/api/check-update")
def api_check_update():
    """Hit GitHub releases API and compare to current version."""
    try:
        r = req.get(
            "https://api.github.com/repos/CryptoWorldGames/kalshibot/releases/latest",
            timeout=8, headers={"User-Agent": "KalshiBot"}
        )
        r.raise_for_status()
        d = r.json()
        latest = d.get("tag_name", "").lstrip("v")
        return jsonify({"ok": True, "latest": latest, "current": BOT_VERSION,
                        "update_available": latest > BOT_VERSION})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "current": BOT_VERSION})


@app.route("/api/version")
def api_version():
    """Bot name + version + uptime, for the web UI header so you always know which
    build is running and how long it's been up."""
    with _bot_lock:
        running = _bot_running
        uptime = (time.time() - _bot_start_time) if (_bot_start_time and running) else 0
    return jsonify({
        "name": BOT_NAME,
        "version": BOT_VERSION,
        "running": running,
        "uptime_secs": int(uptime),
        "server_time": time.time(),  # epoch; frontend formats in the user's timezone
        "hostname": socket.gethostname(),   # which machine is actually running this bot
    })


# Multi-instance safety: only ever ONE bot should trade a given Kalshi account.
# Set KALSHIBOT_PEER_URL on a SECONDARY machine (e.g. on the laptop, point it at
# the home PC's Tailscale URL: http://100.x.y.z:5003). The primary/always-on
# machine leaves it UNSET. Nothing is hardcoded — each user configures their own,
# and unset (the default) means "single instance, no checks" for everyone.
_PEER_URL = os.environ.get("KALSHIBOT_PEER_URL", "").strip().rstrip("/")

@app.route("/api/peer-status")
def api_peer_status():
    """Report whether the OTHER configured instance is up and trading, so the UI can
    warn before running two bots on one account. Unset peer → not configured."""
    if not _PEER_URL:
        return jsonify({"configured": False})
    out = {"configured": True, "url": _PEER_URL,
           "reachable": False, "running": False, "hostname": None}
    try:
        r = req.get(f"{_PEER_URL}/api/version", timeout=4)
        if r.ok:
            d = r.json()
            out["reachable"] = True
            out["running"]   = bool(d.get("running"))
            out["hostname"]  = d.get("hostname")
    except Exception:
        pass  # unreachable peer = treat as down; never block on a network blip
    return jsonify(out)


@app.route("/api/summary")
def api_summary():
    """Aggregate activity (scans / buys / sells / profit) over the past N minutes,
    plus the individual buy & sell events. Powers the Summary tab so you can confirm
    at a glance the bot is alive and trading — no more silent multi-hour gaps."""
    try:
        minutes = float(request.args.get("minutes", 15))
    except (TypeError, ValueError):
        minutes = 15
    minutes = max(1, min(minutes, 60 * 24 * 30))  # clamp 1 min … 30 days
    since = time.time() - minutes * 60

    events = _read_activity(since)
    _log(f"[summary] _read_activity returned {len(events)} events for time window {minutes}m (since {since})")

    scans = buys = sells = settlements = 0
    buy_spent = 0.0
    sell_proceeds = 0.0
    realized_profit = 0.0
    settlement_pnl = 0.0
    have_profit = False
    trades = []  # buy/sell/settlement events for the expandable text list

    scans = sum(1 for e in events if e.get("kind") == "scan")
    act_trades = [e for e in events if e.get("kind") in ("buy", "sell")]

    # PRIMARY SOURCE: Kalshi's own fills history (/portfolio/fills) — complete for
    # the whole account, no matter which machine traded or when the bot was off.
    # The local activity log is the fallback (cold cache / API down) and is used
    # to tag each fill with the bot profile (T1/T2) and a nicer title.
    fills = []
    try:
        fills = _cached_fills_nonblocking()
    except Exception as e:
        print(f"[summary] fills error: {e}")

    try:
        if fills:
            idx = {}
            for e in act_trades:
                idx.setdefault((e.get("ticker"), e.get("side"), e.get("kind")), []).append(e)
            for f in fills:
                try:
                    ts_epoch = datetime.fromisoformat(str(f.get("created_time", "")).replace("Z", "+00:00")).timestamp()
                except (ValueError, AttributeError):
                    continue
                if ts_epoch < since:
                    continue
                kind = "buy" if str(f.get("action", "")).lower() == "buy" else "sell"
                side = str(f.get("side", "")).lower()
                # Try multiple field names — Kalshi API has used different names across versions
                def _cnt(fill):
                    for k in ("count", "contracts", "quantity", "number_of_contracts", "taker_fill_count"):
                        v = fill.get(k)
                        try:
                            fv = float(v)
                            if fv > 0:
                                return fv
                        except (TypeError, ValueError):
                            continue
                    return 0.0
                cnt = _cnt(f)
                # Kalshi fills usually carry yes_price/no_price (cents). Some fills
                # (e.g. market orders) report the executed price under different keys
                # or in dollars — try every known field so we never show "0¢".
                def _price_cents(fill, sd):
                    cand = [
                        fill.get("yes_price") if sd == "yes" else fill.get("no_price"),
                        fill.get("price"), fill.get("avg_price"),
                        fill.get("execution_price"),
                        fill.get("taker_fill_cost"),
                    ]
                    for v in cand:
                        try:
                            fv = float(v)
                        except (TypeError, ValueError):
                            continue
                        if fv > 0:
                            # taker_fill_cost is total cents (divide by count to get per-contract)
                            if v is fill.get("taker_fill_cost") and cnt > 0:
                                return round(fv / cnt)
                            return fv if fv >= 1 else round(fv * 100)  # dollars→cents
                    return 0.0
                price = _price_cents(f, side)
                t = {"kind": kind, "ts": ts_epoch, "ticker": f.get("ticker", ""),
                     "side": side, "count": cnt, "price": price}
                # Attach profile/title/profit from the matching activity-log entry.
                # Try exact (ticker+side+kind) first, then ticker+kind only as fallback.
                def _find_act_match(candidates, ts_ref, window=300):
                    best = None
                    best_dt = window + 1
                    for e in candidates:
                        dt = abs(float(e.get("ts") or 0) - ts_ref)
                        if dt < best_dt:
                            best_dt = dt
                            best = e
                    return best if best_dt <= window else None

                act_match = _find_act_match(idx.get((t["ticker"], side, kind), []), ts_epoch) or \
                            _find_act_match(idx.get((t["ticker"], "yes", kind), []), ts_epoch) or \
                            _find_act_match(idx.get((t["ticker"], "no",  kind), []), ts_epoch)
                if act_match:
                    e = act_match
                    t["profile"] = e.get("profile")
                    t["title"] = e.get("title") or t["ticker"]
                    # Backfill count/price from the activity log when the fill
                    # is missing them — keeps the Summary from showing ×0 @ 0¢.
                    if cnt <= 0 and float(e.get("count") or 0) > 0:
                        cnt = float(e.get("count")); t["count"] = cnt
                    if price <= 0 and float(e.get("price") or 0) > 0:
                        price = float(e.get("price")); t["price"] = price
                    if kind == "sell" and e.get("profit") is not None:
                        t["profit"] = e.get("profit")

                # Fallback: if SELL without profit, try to calculate from matching BUY
                if kind == "sell" and t.get("profit") is None:
                    buy_entries = idx.get((t["ticker"], side, "buy"), [])
                    if buy_entries:
                        # Find the buy that's closest in time (market opened at this buy)
                        best_buy = min(buy_entries, key=lambda b: abs(float(b.get("ts") or 0) - ts_epoch))
                        buy_price = float(best_buy.get("price") or 0)
                        if buy_price > 0:
                            # Profit = (sell_price - buy_price) × count / 100
                            profit = round((price - buy_price) * cnt / 100, 2)
                            t["profit"] = profit

                # Skip fills that still have no count — they're zero-quantity settlement
                # artifacts from Kalshi and produce junk "Qty: 0" rows in the summary.
                if t.get("count", 0) <= 0:
                    continue

                t.setdefault("title", _pretty_title(t["ticker"], ""))
                if kind == "buy":
                    buys += 1
                    spent = cnt * price / 100
                    # Last-resort: use the activity log's recorded spend if we still
                    # couldn't derive a dollar figure from the fill.
                    if spent <= 0:
                        for e in idx.get((t["ticker"], side, "buy"), []):
                            if abs(float(e.get("ts") or 0) - ts_epoch) < 30 and float(e.get("spent") or 0) > 0:
                                spent = float(e.get("spent"))
                                break
                    t["spent"] = round(spent, 2)
                    buy_spent += spent
                else:
                    sells += 1
                    sell_proceeds += cnt * price / 100
                    if t.get("profit") is not None:
                        realized_profit += float(t["profit"])
                        have_profit = True
                trades.append(t)
        else:
            # Fills cache still cold — show the local activity log so the tab is
            # never empty; the 15s auto-refresh upgrades to full Kalshi history.
            for e in act_trades:
                if e.get("kind") == "buy":
                    buys += 1
                    buy_spent += float(e.get("spent") or 0)
                else:
                    sells += 1
                    pr = e.get("profit")
                    if pr is not None:
                        realized_profit += float(pr)
                        have_profit = True
                    sell_proceeds += float(e.get("count") or 0) * float(e.get("price") or 0) / 100
                trades.append(e)
    except Exception as e:
        print(f"[summary] trade merge error: {e}")
    _log(f"[summary] {scans} scans · {buys} buys · {sells} sells ({'kalshi fills' if fills else 'activity log'}) — {len(trades)} trades")

    # Merge settlements from Kalshi's API (don't double-count sold_early entries from activity)
    try:
        since_dt = datetime.fromtimestamp(since, tz=timezone.utc)
        # NONBLOCKING: the blocking fetch takes 30-60s through the rate limiter and the
        # request never completes → the Summary tab renders NOTHING (not even the buys
        # already sitting in the activity log). Serve cached settlements instantly and
        # refresh in the background — the tab auto-refreshes every 15s and fills them in.
        all_settlements = _cached_settlements_nonblocking(hours=24)
        for s in all_settlements:
            # Skip sold_early (already counted as sells in activity log)
            if s.get("result") == "sold_early":
                continue

            # Only include settlements within the time window. Convert the ISO
            # timestamp to an epoch float — the activity log uses epoch floats,
            # and mixing str/float in trades.sort() was the cause of the 500.
            ts_epoch = 0.0
            ts_str = s.get("settled_time", "")
            if ts_str:
                try:
                    ts_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    ts_epoch = ts_dt.timestamp()
                    if ts_dt < since_dt:
                        continue
                except (ValueError, AttributeError):
                    pass

            settlements += 1
            pnl = float(s.get("pnl") or 0)
            if pnl != 0:
                settlement_pnl += pnl

            # Convert settlement to trade format for display
            trade = {
                "kind": "settlement",
                "ts": ts_epoch,
                "ticker": s.get("ticker", ""),
                "title": s.get("title", ""),
                "side": s.get("side", ""),
                "count": s.get("count", 0),
                "price": round(float(s.get("revenue") or 0) / (float(s.get("count") or 1) or 1) * 100, 0) if float(s.get("count") or 0) > 0 else 0,
                "profit": pnl,
                "result": s.get("result", ""),
                "won": s.get("won", False),
                "profile": s.get("profile"),
            }
            trades.append(trade)
    except Exception as e:
        print(f"[summary] settlements merge error: {e}")
        import traceback
        traceback.print_exc()

    # Filter out zombie BUY entries: only hide buys that are 20-60 min old with no
    # corresponding settlement — this is the "Kalshi API still catching up" window.
    # Buys older than 60 min are always shown (settlement API has had time to sync).
    now_epoch = time.time()
    settled_tickers = {s.get("ticker") for s in trades if s.get("kind") == "settlement"}
    sell_tickers = {s.get("ticker") for s in trades if s.get("kind") == "sell"}
    trades = [t for t in trades if not (
        t.get("kind") == "buy" and
        20 * 60 < (now_epoch - float(t.get("ts", 0))) < 60 * 60 and  # 20-60 min window only
        t.get("ticker") not in settled_tickers and
        t.get("ticker") not in sell_tickers
    )]
    # Recount after filtering
    buys = sum(1 for t in trades if t.get("kind") == "buy")
    sells = sum(1 for t in trades if t.get("kind") == "sell")
    settlements = sum(1 for t in trades if t.get("kind") == "settlement")

    # newest first for display — coerce ts to float so one bad row can't 500 the tab
    def _ts_key(x):
        v = x.get("ts", 0)
        if isinstance(v, (int, float)):
            return float(v)
        try:
            return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
        except (ValueError, AttributeError):
            return 0.0
    trades.sort(key=_ts_key)  # ascending order, then reverse for display
    trades.reverse()  # newest first for UI display

    return jsonify({
        "minutes": minutes,
        "since": since,
        "now": time.time(),
        "totals": {
            "scans": scans,
            "buys": buys,
            "sells": sells,
            "settlements": settlements,
            "buy_spent": round(buy_spent, 2),
            "sell_proceeds": round(sell_proceeds, 2),
            "realized_profit": round(realized_profit, 2) if have_profit else None,
            "settlement_pnl": round(settlement_pnl, 2) if settlement_pnl != 0 else None,
        },
        "trades": trades[:2000],  # cap payload (full history can be 800+ fills)
        # Self-diagnostic for the settlements pipeline — lets the UI show WHY
        # settlements are (or aren't) appearing without needing terminal access.
        "settle_debug": dict(_settle_debug),
    })


# ── Restart (works with run_bot.bat — no manager needed) ────────────────────
@app.route("/api/restart", methods=["POST"])
def api_restart():
    """Exit the process on purpose. run_bot.bat's loop then does `git pull` and
    relaunches on the new code — so the ⟳ Update button works from any device
    with ZERO terminal access: push to GitHub → click Update → bot is current."""
    _log("[restart] requested from web UI — exiting so run_bot.bat can pull + relaunch")
    def _die():
        time.sleep(0.8)   # let the HTTP response flush first
        os._exit(0)
    threading.Thread(target=_die, daemon=True).start()
    return jsonify({"ok": True, "msg": "Restarting — run_bot.bat will git pull and relaunch (~15s)"})


# ── Smart Strategy endpoint ──────────────────────────────────────────────────
@app.route("/api/smart-strategy", methods=["GET", "POST"])
def api_smart_strategy():
    """Master switch for auto Scanner/Lotto mode based on live crypto spread.
    GET: return current setting + live spread analysis.
    POST: set enabled state and auto-toggle bots if enabled."""
    global active_profiles

    # Calculate live average spread
    avg_spread = _average_crypto_spread()
    recommended = "scanner" if avg_spread >= smart_strategy["spread_threshold"] else "lotto"

    if request.method == "GET":
        return jsonify({
            "enabled": smart_strategy["enabled"],
            "spread_threshold": smart_strategy["spread_threshold"],
            "current_spread": round(avg_spread, 2),
            "recommended_mode": recommended,
            "active_mode": recommended if smart_strategy["enabled"] else None,
        })

    # POST: update setting and auto-toggle bots if enabled
    data = request.get_json(silent=True) or {}
    if "enabled" in data:
        smart_strategy["enabled"] = bool(data["enabled"])
        # Auto-toggle bots based on spread if enabling strategy
        if smart_strategy["enabled"]:
            if avg_spread >= smart_strategy["spread_threshold"]:
                # Spread >= $100: use Scanner only
                active_profiles = ["T1"]
                _log(f"[smart-strategy] spread ${avg_spread} >= ${smart_strategy['spread_threshold']} → SCANNER mode")
            else:
                # Spread < $100: use Lotto only (both sides = insurance)
                active_profiles = ["T2"]
                _log(f"[smart-strategy] spread ${avg_spread} < ${smart_strategy['spread_threshold']} → LOTTO mode (insurance hedge)")
            _save_profiles()
        else:
            # Strategy disabled: allow manual control (don't change active_profiles)
            _log("[smart-strategy] disabled → manual mode")

    if "spread_threshold" in data:
        try:
            thr = float(data["spread_threshold"])
            if 10 <= thr <= 1000:  # reasonable range
                smart_strategy["spread_threshold"] = thr
        except (TypeError, ValueError):
            pass

    _save_smart_strategy()
    _log(f"[smart-strategy] updated: enabled={smart_strategy['enabled']}, threshold=${smart_strategy['spread_threshold']}")

    # Return updated state
    return jsonify({
        "enabled": smart_strategy["enabled"],
        "spread_threshold": smart_strategy["spread_threshold"],
        "current_spread": round(avg_spread, 2),
        "recommended_mode": recommended,
        "active_mode": recommended if smart_strategy["enabled"] else None,
        "active_profiles": active_profiles,
    })


@app.route("/api/smart-strategy/status")
def api_smart_strategy_status():
    """Current scan state: which token each profile is analyzing.
    Returns: {T1: {token, timestamp}, T2: {token, timestamp}, per_token_spreads: {...}}"""
    return jsonify({
        "T1": _scan_state["T1"],
        "T2": _scan_state["T2"],
        "per_token_spreads": _scan_state["per_token_spreads"],
        "active_profiles": active_profiles,
    })


def _per_token_payload(token, recommendations):
    """One token's settings + live AI recommendation + per-GAME averages + volatility."""
    cfg = per_token_settings[token]
    avg4h, n4h = _avg_spread_games(token, _GAMES_4H)
    avg24h, n24h = _avg_spread_games(token, _MAX_GAMES)
    vol, vol_pct, vol_elevated = _spread_volatility(token, _GAMES_4H)
    return {
        "enabled": cfg.get("enabled", False),
        "buy_last_min": cfg.get("buy_last_min", 1),
        "threshold": cfg.get("threshold", 100),
        "mode": cfg.get("mode", "both"),
        "order_type": cfg.get("order_type", "market"),
        "ai_choice": cfg.get("ai_choice", True),
        "ai_recommended_spread": recommendations.get(token, 100),
        "avg_spread_4h": avg4h,   "avg_games_4h": n4h,    # last 16 games
        "avg_spread_24h": avg24h, "avg_games_24h": n24h,  # last 96 games
        "volatility": vol, "volatility_pct": vol_pct, "vol_elevated": vol_elevated,
    }


@app.route("/api/smart-strategy/per-token", methods=["GET", "POST"])
def api_per_token_settings():
    """Per-token Smart Strategy configuration and AI recommendations.
    GET: return current per-token settings and safe spread recommendations.
    POST: update per-token settings from frontend localStorage."""

    if request.method == "GET":
        # Get live token spreads for recommendation calculation
        token_spreads = _token_spread_cache.get("data", [])
        recommendations = _compute_safe_spread_recommendations(token_spreads)

        # Build response with current settings + recommendations
        result = {token: _per_token_payload(token, recommendations) for token in per_token_settings}
        return jsonify({"settings": result})

    # POST: update per-token settings
    data = request.get_json(silent=True) or {}
    if "settings" in data:
        try:
            new_settings = data["settings"]
            for token in per_token_settings:
                if token in new_settings:
                    token_cfg = new_settings[token]
                    # Validate and update each field
                    if "enabled" in token_cfg:
                        per_token_settings[token]["enabled"] = bool(token_cfg["enabled"])
                    if "buy_last_min" in token_cfg:
                        per_token_settings[token]["buy_last_min"] = float(token_cfg["buy_last_min"])
                    if "threshold" in token_cfg:
                        per_token_settings[token]["threshold"] = float(token_cfg["threshold"])
                    if "mode" in token_cfg and token_cfg["mode"] in ("scanner", "lotto", "both"):
                        per_token_settings[token]["mode"] = token_cfg["mode"]
                    if "order_type" in token_cfg and token_cfg["order_type"] in ("market", "limit"):
                        per_token_settings[token]["order_type"] = token_cfg["order_type"]
                    if "ai_choice" in token_cfg:
                        per_token_settings[token]["ai_choice"] = bool(token_cfg["ai_choice"])
            _save_per_token_settings()
            _log("[per-token] settings updated from frontend")
        except Exception as e:
            _log(f"[per-token] settings update error: {e}")
            return jsonify({"error": str(e)}), 400

    # Return updated state + recommendations
    token_spreads = _token_spread_cache.get("data", [])
    recommendations = _compute_safe_spread_recommendations(token_spreads)
    result = {token: _per_token_payload(token, recommendations) for token in per_token_settings}
    return jsonify({"settings": result})


@app.route("/api/audit")
def api_audit():
    """Full trading audit over the past N days: where the money actually went.
    Combines the activity log (buys/sells) with Kalshi settlements (authoritative
    closes) using the REAL fee formula. Answers 'why is my 30-day P&L -$X?'."""
    try:
        days = min(30, max(1, int(request.args.get("days", 30))))
    except (TypeError, ValueError):
        days = 30
    since = time.time() - days * 86400

    buys = sells = 0
    buy_spent = sell_gross = sell_fees = 0.0
    for e in _read_activity(since):
        k = e.get("kind")
        if k == "buy":
            buys += 1
            buy_spent += float(e.get("spent") or 0)
        elif k == "sell":
            sells += 1
            cnt = float(e.get("count") or 0)
            px  = float(e.get("price") or 0)
            sell_gross += cnt * px / 100
            sell_fees  += _kalshi_fee(cnt, px)

    wins = losses = 0
    win_dollars = loss_dollars = settle_fees = 0.0
    worst = []
    try:
        for s in _cached_settlements(hours=days * 24):
            pnl = float(s.get("pnl") or 0)
            cnt = float(s.get("count") or 0)
            cost = float(s.get("cost") or 0)
            settle_fees += _kalshi_fee(cnt, (cost / cnt * 100) if cnt > 0.001 else 0)
            if pnl > 0.001:
                wins += 1
                win_dollars += pnl
            elif pnl < -0.001:
                losses += 1
                loss_dollars += abs(pnl)
            worst.append({"ticker": s.get("ticker"), "title": s.get("title"),
                          "pnl": round(pnl, 2), "cost": round(cost, 2),
                          "result": s.get("result"), "settled_time": s.get("settled_time")})
    except Exception as e:
        print(f"[audit] settlements error: {e}")
    worst.sort(key=lambda x: x["pnl"])

    total = wins + losses
    net = round(win_dollars - loss_dollars, 2)
    return jsonify({
        "days": days,
        "activity": {
            "buys": buys, "buy_spent": round(buy_spent, 2),
            "sells": sells, "sell_gross": round(sell_gross, 2),
            "avg_buy_size": round(buy_spent / buys, 2) if buys else 0,
        },
        "settlements": {
            "count": total, "wins": wins, "losses": losses,
            "win_rate_pct": round(wins / total * 100, 1) if total else None,
            "won_dollars": round(win_dollars, 2),
            "lost_dollars": round(loss_dollars, 2),
            "profit_factor": round(win_dollars / loss_dollars, 2) if loss_dollars > 0.01 else None,
            "net_pnl": net,
        },
        "fees_estimated": round(sell_fees + settle_fees, 2),
        "worst_10_trades": worst[:10],
        "best_10_trades": sorted(worst, key=lambda x: -x["pnl"])[:10],
        "note": "settlement P&L uses real Kalshi fee formula ceil(0.07*C*P*(1-P)); "
                "a profit_factor below 1.0 means the strategy is losing money",
    })


@app.route("/api/export/trades.csv")
def api_export_trades():
    """CSV export of buys, sells, and settlements for spreadsheet audit."""
    try:
        days = min(90, max(1, int(request.args.get("days", 30))))
    except (TypeError, ValueError):
        days = 30
    since = time.time() - days * 86400
    lines = ["type,time_utc,ticker,title,side,count,price_cents,dollars,pnl,result,profile"]
    def _csv(v):
        s = str(v if v is not None else "")
        return '"' + s.replace('"', '""') + '"' if ("," in s or '"' in s) else s
    for e in _read_activity(since):
        k = e.get("kind")
        if k not in ("buy", "sell"):
            continue
        t = datetime.fromtimestamp(float(e.get("ts") or 0), tz=timezone.utc).isoformat()
        amt = e.get("spent") if k == "buy" else round(float(e.get("count") or 0) * float(e.get("price") or 0) / 100, 2)
        lines.append(",".join(_csv(x) for x in [
            k, t, e.get("ticker"), e.get("title"), e.get("side"), e.get("count"),
            e.get("price"), amt, e.get("profit"), e.get("reason"), e.get("profile")]))
    try:
        for s in _cached_settlements(hours=days * 24):
            lines.append(",".join(_csv(x) for x in [
                "settlement", s.get("settled_time"), s.get("ticker"), s.get("title"),
                s.get("side"), s.get("count"), "", s.get("revenue"), s.get("pnl"),
                s.get("result"), s.get("profile")]))
    except Exception:
        pass
    from flask import Response
    return Response("\n".join(lines), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=kalshibot_trades_{days}d.csv"})


@app.route("/api/enrich-positions", methods=["GET"])
def enrich_positions():
    """Fetch market data for specific position tickers (called after initial fast load).

    Query params:
    - tickers: comma-separated list (e.g., "TICKER1,TICKER2,TICKER3")

    Returns: { "ticker": {...enriched market data...}, ... }
    """
    ticker_str = request.args.get("tickers", "")
    if not ticker_str:
        return jsonify({})

    tickers = [t.strip() for t in ticker_str.split(",") if t.strip()]
    result = {}

    # Split into cached (instant) vs uncached (need fetching). Serving cached
    # markets from the 60s cache costs zero API calls and never times out.
    now = time.time()
    uncached = []
    markets_by_ticker = {}
    for ticker in tickers:
        c = _market_cache.get(ticker)
        if c and (now - c["ts"]) < _MARKET_CACHE_TTL:
            markets_by_ticker[ticker] = c["data"]
        else:
            uncached.append(ticker)

    # Fetch ALL uncached tickers using Kalshi's BATCH endpoint: GET /markets?tickers=A,B,C
    # returns up to 100 markets in ONE call. This replaces the old loop that made one
    # /markets/{ticker} call per ticker — that loop was throttled by the rate limiter
    # and routinely timed out the frontend (the "blanks" / dashes bug). One batched
    # call per 100 tickers fills every position's price reliably and fast.
    for i in range(0, len(uncached), 100):
        chunk = uncached[i:i + 100]
        try:
            data = kalshi_get("/markets", {"tickers": ",".join(chunk), "limit": 100})
            for m in data.get("markets", []):
                tk = m.get("ticker", "")
                if tk:
                    markets_by_ticker[tk] = m
                    _market_cache[tk] = {"data": m, "ts": now}  # warm the cache
        except Exception as e:
            print(f"[enrich-positions] batch fetch error: {e}")

    for ticker in tickers:
        mkt = markets_by_ticker.get(ticker)
        if not mkt:
            result[ticker] = {}
            continue
        try:
            event_ticker = mkt.get("event_ticker", "")
            result[ticker] = {
                "event_ticker": event_ticker,
                "title": _pretty_title(ticker, mkt.get("title") or _title_cache.get(ticker) or _humanize_ticker(ticker)),
                "category": mkt.get("category", ""),
                "current_yes": _mark_price_cents(mkt, "yes"),
                "current_no": _mark_price_cents(mkt, "no"),
                "close_time": mkt.get("close_time") or mkt.get("expiration_time"),
                "kalshi_url": _kalshi_url(event_ticker, ticker),
            }
        except Exception:
            result[ticker] = {}

    return jsonify(result)


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
                yes_cnt, no_cnt, yes_cost, no_cost = _settle_counts(s)
                rev      = float(s.get("revenue")  or 0) / 100
                cost     = yes_cost if yes_cnt > 0.001 else no_cost
                cnt      = yes_cnt if yes_cnt > 0.001 else no_cnt
                fee      = _kalshi_fee(cnt, (cost / cnt * 100) if cnt > 0.001 else 0)
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
    total_pnl_all = 0.0
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
        total_pnl_all += d["total_pnl"]

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
        "total_pnl":      round(total_pnl_all, 2),
        "scan_log_count": scan_log_count,
    })


@app.route("/api/coach")
def coach():
    """Stats analyzer over tracked positions, scan log, and recent settlements.
    Returns structured insights + recommendations the frontend renders in the Coach tab."""
    from collections import defaultdict
    import json

    # Parse filter settings from frontend (optional)
    filters = {}
    try:
        filters_json = request.args.get("filters", "{}")
        filters = json.loads(filters_json) if filters_json else {}
    except Exception:
        pass

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
                yes_cnt, no_cnt, yes_cost, no_cost = _settle_counts(s)
                if yes_cnt > 0.001 and no_cnt > 0.001:
                    continue  # hedged
                rev  = float(s.get("revenue") or 0) / 100
                cost = yes_cost if yes_cnt > 0.001 else no_cost
                cnt  = yes_cnt if yes_cnt > 0.001 else no_cnt
                fee  = _kalshi_fee(cnt, (cost / cnt * 100) if cnt > 0.001 else 0)
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
    profs    = defaultdict(lambda: {"n":0,"wins":0,"pnl":0.0})   # by bot tab (T1/T2)
    # Odds-band performance for the Lotto bot specifically (T2 long-shots).
    lotto_bands = defaultdict(lambda: {"n":0,"wins":0,"pnl":0.0})

    total_n = 0
    total_wins = 0
    total_pnl  = 0.0

    for ticker, pos in snap.items():
        if pos.get("status") != "sold": continue
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

        prof = pos.get("profile") or "T1"
        for bucket, key in [(cats,cat), (bands, price_band(bp)), (sides, side), (profs, prof)]:
            bucket[key]["n"]    += 1
            bucket[key]["pnl"]  += pnl
            if won: bucket[key]["wins"] += 1
        if prof == "T2":  # Lotto bot — track which odds bands actually hit
            lb = lotto_bands[price_band(bp)]
            lb["n"] += 1; lb["pnl"] += pnl
            if won: lb["wins"] += 1
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
    profile_rows = rows_from(profs)        # T1 vs T2 head-to-head
    lotto_band_rows = rows_from(lotto_bands)  # Lotto bot's odds-band hit rates

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

    # ── Lotto bot (T2) odds insight ──────────────────────────────────────────
    lotto_total = profs.get("T2", {}).get("n", 0)
    if lotto_total >= sample_min:
        l_wins = profs["T2"]["wins"]; l_pnl = profs["T2"]["pnl"]
        l_wr = round(l_wins / lotto_total * 100, 1)
        recs.append({"type": ("prefer" if l_pnl > 0 else "avoid"),
                     "msg": f"🎟️ Lotto bot (T2): {l_wr}% of {lotto_total} long-shots hit, "
                            f"{'+' if l_pnl>=0 else ''}${l_pnl:.2f} total."})
        best_l = [r for r in lotto_band_rows if r["n"] >= sample_min and r["total_pnl"] > 0]
        if best_l:
            b = best_l[0]
            recs.append({"type":"prefer", "msg": f"🎟️ Lotto's best odds band: **{b['key']}** "
                                                 f"({b['win_rate']}% hit, +${b['total_pnl']:.2f}) — lean cheaper/dearer toward it."})

    if total_n < 5:
        recs.append({"type":"info", "msg": f"Sample size is small ({total_n} trades) — recommendations get sharper with more data."})

    # ── Settings-based tips ──────────────────────────────────────────────────────
    # Analyze current configuration for obvious issues or improvements

    # Check sell strategy settings
    if sell_strategy.get("mode") == "profit":
        target_pct = sell_strategy.get("target_pct")
        if target_pct and target_pct < 2:
            recs.append({"type":"tip", "msg": f"💡 Your profit target is {target_pct}% — very low! Try 5-10% for better risk/reward."})

    if sell_strategy.get("target_dollars") and sell_strategy.get("target_dollars") < 0.01:
        recs.append({"type":"tip", "msg": f"💡 Dollar profit target is tiny ($0.01) — you'll miss bigger wins. Try $0.10+"})

    # Check stop-loss conflicts
    stop_loss_pct = sell_strategy.get("stop_loss_pct")
    target_pct = sell_strategy.get("target_pct") or 10
    if stop_loss_pct and stop_loss_pct > 0 and stop_loss_pct > target_pct * 2:
        recs.append({"type":"tip", "msg": f"⚠️ Stop-loss ({stop_loss_pct}%) >> profit target ({target_pct}%) — you'll hit losses before wins!"})

    # Check target price conflicts
    target_price_c = sell_strategy.get("target_price_cents")
    buy_in_price_c = sell_strategy.get("buy_in_price_cents")
    if target_price_c and buy_in_price_c:
        if target_price_c <= buy_in_price_c and target_pct is None:  # shorting without checking if intentional
            recs.append({"type":"tip", "msg": f"ℹ️ Selling at {target_price_c}¢ when you buy at {buy_in_price_c}¢ (shorting). Verify this is intentional."})

    # Check if tracking positions exist
    open_count = len([p for p in snap.values() if p.get("status") == "open"])
    if open_count == 0 and total_n < 3:
        recs.append({"type":"info", "msg": "💡 No active positions yet. Hit **Start Bot** to begin buying — Coach gets smarter with trade data."})

    # Performance-based behavioral tips
    if total_n >= 5 and total_wins > 0:
        win_rate_pct = (total_wins / total_n) * 100
        if win_rate_pct > 75:
            recs.append({"type":"tip", "msg": f"✅ Your {win_rate_pct:.0f}% win rate is excellent! Consider tightening stop-loss to lock in gains."})
        elif win_rate_pct < 40:
            recs.append({"type":"tip", "msg": f"📊 Your {win_rate_pct:.0f}% win rate is low. Check if you're buying too conservatively or holding losers too long."})

    # ── Filter-based tips (from frontend settings) ───────────────────────────────
    if filters:
        buy_min = filters.get("buyMin", 80)
        buy_max = filters.get("buyMax", 96)
        time_window = filters.get("timeWindow", 15)
        buy_amount = filters.get("buyAmount", 1.0)

        # Tip: Buy range too narrow
        range_width = buy_max - buy_min
        if range_width < 10:
            recs.append({"type":"tip", "msg": f"🎯 Your buy range ({buy_min}-{buy_max}%) is very tight. Widen to {buy_min-10}-{buy_max}% to find more opportunities."})

        # Tip: Buy range too high
        if buy_min > 85:
            recs.append({"type":"tip", "msg": f"🎯 You're buying conservatively ({buy_min}-{buy_max}%). Try 50-96% to catch more profitable moves."})

        # Tip: Time window too restrictive
        if time_window < 20:
            recs.append({"type":"tip", "msg": f"⏱️ {time_window}-min window is restrictive. Expand to 15-60 min for more trading opportunities."})

        # Tip: Buy amount might be too low
        if buy_amount < 0.5 and total_n >= 5:
            recs.append({"type":"tip", "msg": f"💰 Buy amount (${buy_amount:.2f}) is very small. Try $1-5 for meaningful position sizing."})

        # Tip: Category filters
        cats_enabled = sum([
            1 if filters.get("showCrypto") else 0,
            1 if filters.get("showSports") else 0,
            1 if filters.get("showPolitics") else 0,
            1 if filters.get("showEconomics") else 0,
        ])
        if cats_enabled == 0:
            recs.append({"type":"tip", "msg": "📂 All categories are disabled! Enable at least one to start buying."})
        elif cats_enabled == 1:
            recs.append({"type":"tip", "msg": "📂 Only 1 category enabled. Enable 2+ for more variety and diversification."})

        # Tip: Check if profitable categories are disabled
        for cat_row in cat_rows:
            cat_key = cat_row.get("key", "").lower()
            pnl = cat_row.get("total_pnl", 0)
            if pnl > 2.0:  # profitable category
                if cat_key == "crypto" and not filters.get("showCrypto"):
                    recs.append({"type":"tip", "msg": f"💡 **Crypto** is +${pnl:.2f} profitable but disabled! Enable it."})
                elif cat_key == "sports" and not filters.get("showSports"):
                    recs.append({"type":"tip", "msg": f"💡 **Sports** is +${pnl:.2f} profitable but disabled! Enable it."})
                elif cat_key == "politics" and not filters.get("showPolitics"):
                    recs.append({"type":"tip", "msg": f"💡 **Politics** is +${pnl:.2f} profitable but disabled! Enable it."})
                elif cat_key == "economics" and not filters.get("showEconomics"):
                    recs.append({"type":"tip", "msg": f"💡 **Economics** is +${pnl:.2f} profitable but disabled! Enable it."})

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
        "by_profile":    profile_rows,      # T1 (Scanner) vs T2 (Lotto)
        "lotto_odds":    lotto_band_rows,   # Lotto bot's hit rate by odds band
        "recommendations": recs,
        "strategies": strategies,
    })


# ---------------------------------------------------------------------------
# Bot control endpoints (start/stop/status) — headless trading loop
# ---------------------------------------------------------------------------

@app.route("/api/bot/start", methods=["POST"])
def bot_start():
    """Start the headless auto-trading loop."""
    global _bot_running, _bot_start_time
    with _bot_lock:
        if _bot_running:
            return jsonify({"error": "Bot already running"}), 409
        _bot_running = True
        _bot_start_time = time.time()
    _save_bot_config(True)  # Persist so it restarts if home PC reboots
    print("[bot] Trading loop started")
    return jsonify({"ok": True, "status": "running"})


@app.route("/api/bot/stop", methods=["POST"])
def bot_stop():
    """Stop the headless auto-trading loop gracefully."""
    global _bot_running
    with _bot_lock:
        if not _bot_running:
            return jsonify({"error": "Bot not running"}), 409
        _bot_running = False
    _save_bot_config(False)  # Persist so it doesn't restart if home PC reboots
    print("[bot] Trading loop stopped")
    return jsonify({"ok": True, "status": "stopped"})


@app.route("/api/apilog", methods=["GET"])
def api_log():
    """Recent OUTBOUND Kalshi API calls + a 60s summary, for the API Log tab so you
    can watch how busy the bot is and see rate-limiting (429s) as it happens."""
    now = time.time()
    rows = list(_api_log)
    last60 = [e for e in rows if now - e["ts"] <= 60]
    by_ep = {}
    for e in last60:
        key = f'{e["method"]} {e["ep"].split("?")[0]}'
        by_ep[key] = by_ep.get(key, 0) + 1
    top = sorted(by_ep.items(), key=lambda kv: -kv[1])[:8]
    return jsonify({
        "now": now,
        "calls_60s": len(last60),
        "errors_60s": sum(1 for e in last60 if e["status"] >= 400),
        "rate_limited_60s": sum(1 for e in last60 if e["status"] == 429),
        "top_endpoints_60s": top,
        "recent": rows[-120:][::-1],   # newest first
    })

@app.route("/api/bot/status", methods=["GET"])
def bot_status():
    """Get current bot status: running, uptime, total bought this session."""
    with _bot_lock:
        running = _bot_running
        start_time = _bot_start_time

    uptime_seconds = 0
    if running and start_time:
        uptime_seconds = int(time.time() - start_time)

    # Count buys in this session (positions with bot_bought=True)
    with _lock:
        buys_this_session = sum(1 for p in tracked.values() if p.get("bot_bought"))

    return jsonify({
        "running": running,
        "uptime_seconds": uptime_seconds,
        "total_bought_session": buys_this_session,
        "start_time": start_time,
    })


def _already_running(port: int = 5003) -> bool:
    """True if another KalshiBot is already serving on this port. Prevents the
    duplicate-instance problem (two bots double the API traffic and trip 429s).
    T1 + T2 (and other profiles) still run together inside this ONE process —
    this only blocks a SECOND terminal/process."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.4)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()

def _print_banner():
    """Print a name/version banner AND set the terminal window title, so the bot's
    identity is visible at all times (the title bar persists even after scrolling)."""
    title = f"{BOT_NAME} v{BOT_VERSION}"
    # OSC escape sets the terminal/window title (works in Windows Terminal, xterm, etc.)
    try:
        sys.stdout.write(f"\033]0;{title}\007")
        sys.stdout.flush()
    except Exception:
        pass
    # Windows cmd.exe fallback
    try:
        if os.name == "nt":
            os.system(f"title {title}")
    except Exception:
        pass
    bar = "═" * 52
    print(f"╔{bar}╗")
    print(f"║  {title:<48}  ║")
    print(f"║  {'Kalshi auto-trading bot':<48}  ║")
    print(f"╚{bar}╝")


# ── Hands-off auto-updater ───────────────────────────────────────────────────
# Polls GitHub for new commits on the CURRENT branch and, when it finds any,
# exits the process. run_bot.bat's loop then does `git reset --hard` + relaunch,
# so the home PC updates itself with ZERO interaction — push from anywhere
# (e.g. a laptop while travelling) and the bot is current within a few minutes.
# Disable with KALSHIBOT_AUTOUPDATE=0; tune the interval with
# KALSHIBOT_AUTOUPDATE_SECS (default 300).
_AUTOUPDATE_ENABLED  = os.environ.get("KALSHIBOT_AUTOUPDATE", "1") != "0"
_AUTOUPDATE_INTERVAL = int(os.environ.get("KALSHIBOT_AUTOUPDATE_SECS", "300"))

def _git_cmd(*args, timeout=30):
    return subprocess.run(["git", "-C", str(HERE), *args],
                          capture_output=True, text=True, timeout=timeout)

def _tailscale_ip():
    """Return this machine's Tailscale IPv4 (100.x.y.z) if Tailscale is installed
    and connected, else None. Used to print the URL for reaching the bot's web UI
    from any other device on your tailnet (laptop/phone, anywhere in the world)."""
    # 1) Ask the Tailscale CLI directly (most reliable on Windows/Mac/Linux)
    for exe in ("tailscale", r"C:\Program Files\Tailscale\tailscale.exe"):
        try:
            out = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True, timeout=5)
            lines = (out.stdout or "").strip().splitlines()
            if lines and lines[0].strip().startswith("100."):
                return lines[0].strip()
        except (FileNotFoundError, OSError, subprocess.SubprocessError):
            continue
    # 2) Fallback: scan local interfaces for the Tailscale CGNAT range 100.64.0.0/10
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            addr = info[4][0]
            if addr.startswith("100."):
                try:
                    if 64 <= int(addr.split(".")[1]) <= 127:
                        return addr
                except (ValueError, IndexError):
                    continue
    except Exception:
        pass
    return None

def _auto_update_loop():
    try:
        branch = _git_cmd("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    except Exception as e:
        print(f"[auto-update] could not detect branch ({e}) — disabled this run")
        return
    if not branch or branch == "HEAD":
        print("[auto-update] detached HEAD / no branch — auto-update disabled this run")
        return
    print(f"[auto-update] watching origin/{branch} every {_AUTOUPDATE_INTERVAL}s")
    while True:
        time.sleep(_AUTOUPDATE_INTERVAL)
        try:
            if _git_cmd("fetch", "origin", branch).returncode != 0:
                continue
            behind = _git_cmd("rev-list", "--count", f"HEAD..origin/{branch}").stdout.strip()
            if behind.isdigit() and int(behind) > 0:
                print(f"[auto-update] {behind} new commit(s) on origin/{branch} — restarting to apply")
                os._exit(0)   # run_bot.bat resets --hard to origin/branch and relaunches
        except Exception as e:
            print(f"[auto-update] check failed: {e}")


if __name__ == "__main__":
    _print_banner()
    if _already_running(5003):
        print("=" * 60)
        print(f"{BOT_NAME} is ALREADY RUNNING (port 5003 is in use).")
        print("This window will close — use the one that's already open,")
        print("or open http://localhost:5003 in your browser.")
        print("=" * 60)
        try:
            input("Press Enter to close...")
        except EOFError:
            pass
        sys.exit(0)
    print("Open http://localhost:5003")
    _ts_ip = _tailscale_ip()
    if _ts_ip:
        print(f"📱 Remote (Tailscale): http://{_ts_ip}:5003")
        print("   ^ Open this from your laptop/phone anywhere — see TAILSCALE_SETUP.md")
    else:
        print("   (Tailscale not detected — see TAILSCALE_SETUP.md to control the bot remotely)")

    # Health check: attempt Kalshi API connection, but don't block startup if it fails
    try:
        bal = kalshi_get("/portfolio/balance")
        if not bal or "balance" not in bal:
            print("\n⚠️  WARNING: Kalshi API returned unexpected response — check credentials and network.\n")
        else:
            print(f"✓ Kalshi API reachable (balance: ${float(bal.get('balance_dollars') or 0):.2f})")
    except Exception as e:
        print(f"\n⚠️  WARNING: Cannot reach Kalshi API: {e}")
        print("Bot will start, but will be unable to trade until the connection is restored.\n")

    # Start the hands-off auto-updater (no-op if KALSHIBOT_AUTOUPDATE=0)
    if _AUTOUPDATE_ENABLED:
        threading.Thread(target=_auto_update_loop, daemon=True).start()

    # threaded=True is critical: the scan loop and slow Kalshi API calls can each
    # tie up a worker for seconds at a time. Single-threaded (the Werkzeug default)
    # means a phone/laptop page-load (GET /) queues behind that work and times out,
    # so the browser falls back to its cached "offline copy" snapshot — even though
    # the bot is running fine. Multi-threaded lets navigation + API + scan run at
    # once so the UI is always reachable.
    app.run(debug=False, host="0.0.0.0", port=5003, threaded=True)


# ─────────────────────────────────────────────────────────────────────────────
# GROUP EXIT STRATEGY — Group positions by expiration, auto-sell losers, 
# hold winners until group profit target is met, optional limited martingale
# ─────────────────────────────────────────────────────────────────────────────

def _group_positions_by_expiration(positions: list) -> dict:
    """
    Group positions by close_time (expiration).
    Returns: {
        "2026-05-31T21:00:00Z": [pos1, pos2, ...],
        "2026-06-01T21:00:00Z": [pos3, pos4, ...],
    }
    """
    groups = {}
    for pos in positions:
        expiry = pos.get("close_time") or ""
        if expiry:
            if expiry not in groups:
                groups[expiry] = []
            groups[expiry].append(pos)
    return groups


def _calc_group_pnl(group_positions: list) -> dict:
    """
    Calculate P&L for a group of positions.
    Returns: {
        "total_profit": float (in dollars),
        "winning": [list of positions with profit > 0],
        "losing": [list of positions with profit < 0],
        "break_even": [list of positions with profit ≈ 0],
    }
    """
    total = 0.0
    winning = []
    losing = []
    break_even = []
    
    for pos in group_positions:
        current_yes = pos.get("current_yes")
        current_no = pos.get("current_no")
        buy_price = pos.get("buy_price")
        qty = abs(pos.get("quantity", 0))
        
        if not qty or buy_price is None:
            continue
        
        side = "yes" if pos.get("quantity", 0) > 0 else "no"
        current_price = current_yes if side == "yes" else current_no
        
        if current_price is None:
            continue
        
        profit_cents = qty * (current_price - buy_price)
        profit_dollars = profit_cents / 100
        total += profit_dollars
        
        if profit_dollars > 0.005:  # small buffer for break-even
            winning.append({**pos, "profit": profit_dollars})
        elif profit_dollars < -0.005:
            losing.append({**pos, "profit": profit_dollars})
        else:
            break_even.append({**pos, "profit": profit_dollars})
    
    return {
        "total_profit": round(total, 2),
        "winning": winning,
        "losing": losing,
        "break_even": break_even,
    }


@app.route("/api/group-exits", methods=["GET"])
def group_exits_analysis():
    """
    Analyze positions grouped by expiration and recommend exits.
    
    Returns: {
        "groups": {
            "2026-05-31T21:00:00Z": {
                "total_profit": -0.50,
                "winning": [...],
                "losing": [...],
                "recommendation": "sell_losers_below_5"  # or "hold_all", "sell_all", etc.
            },
            ...
        }
    }
    """
    try:
        # Get settings
        auto_sell_losers = request.args.get("auto_sell_losers", "true").lower() == "true"
        loss_threshold = float(request.args.get("loss_threshold", 0.50))  # max loss per position
        group_profit_target = float(request.args.get("group_profit_target", 0.25))  # total profit target
        
        # Fetch positions
        bal_data = kalshi_get("/portfolio/balance")
        positions = []
        cursor = None
        while True:
            params = {"count": 200}
            if cursor:
                params["cursor"] = cursor
            pos_data = kalshi_get("/portfolio/positions", params)
            batch = pos_data.get("market_positions", pos_data.get("positions", []))
            if not batch:
                break
            for p in batch:
                ticker = p.get("market_id") or p.get("ticker", "")
                if not ticker:
                    continue
                qty_raw = p.get("position_fp", p.get("position", p.get("quantity_owned", 0)))
                try:
                    qty = float(qty_raw)
                except (TypeError, ValueError):
                    qty = 0
                if abs(qty) < 0.001:
                    continue
                
                # Get market data
                try:
                    mkt = _get_market(ticker)
                    current_yes = _mark_price_cents(mkt, "yes")
                    current_no = _mark_price_cents(mkt, "no")
                    close_time = mkt.get("close_time") or mkt.get("expiration_time", "")
                except Exception:
                    current_yes = None
                    current_no = None
                    close_time = ""
                
                # Get buy price from tracking
                bot_info = tracked.get(ticker)
                buy_price = bot_info.get("buy_price") if bot_info else None
                
                positions.append({
                    "ticker": ticker,
                    "quantity": qty,
                    "current_yes": current_yes,
                    "current_no": current_no,
                    "buy_price": buy_price,
                    "close_time": close_time,
                    "bot_bought": bot_info is not None,
                })
            
            cursor = pos_data.get("cursor")
            if not cursor:
                break
        
        # Group by expiration
        groups = _group_positions_by_expiration(positions)
        
        result = {}
        for expiry, group_pos in groups.items():
            analysis = _calc_group_pnl(group_pos)
            
            # Recommendation logic
            recommendation = "hold"
            if analysis["losing"] and auto_sell_losers:
                if analysis["total_profit"] < -loss_threshold:
                    # Sell losers to cut losses
                    recommendation = "sell_losers"
                elif len(analysis["winning"]) > 0 and len(analysis["losing"]) > len(analysis["winning"]) * 2:
                    # More than 2x losers as winners - sell bottom performers
                    recommendation = "sell_underperformers"
            
            if analysis["total_profit"] >= group_profit_target:
                recommendation = "sell_all_take_profit"
            
            result[expiry] = {
                "total_profit": analysis["total_profit"],
                "winning_count": len(analysis["winning"]),
                "losing_count": len(analysis["losing"]),
                "total_positions": len(group_pos),
                "recommendation": recommendation,
                "winning": [{"ticker": p["ticker"], "profit": p["profit"]} for p in analysis["winning"]],
                "losing": [{"ticker": p["ticker"], "profit": p["profit"]} for p in analysis["losing"]],
            }
        
        return jsonify({"groups": result})
    except Exception as e:
        print(f"[group-exits] error: {e}")
        return jsonify({"error": str(e)}), 500
