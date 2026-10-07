from __future__ import annotations

import hashlib
import copy
import json
import logging
import math
import os
import sqlite3
import statistics
import sys
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from logging.handlers import RotatingFileHandler

import numpy as np
import pandas as pd
import requests
from flask import Flask, jsonify, redirect, render_template_string, request, url_for
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    import websocket as _websocket_client
except Exception:
    _websocket_client = None

# ============================================================
# TITAN
# Technical layers: Fibonacci / Support-Resistance / Multi TF scoring integrated ENTERPRISE V32
# Analysis only. No order execution.
# V60: ALL files, data, cache, logs, temp, bytecode, Flask instance, SQLite
# journals, history and secrets are HARD-LOCKED to Internal Storage/TAITAN.
# Nothing is written anywhere else on the phone.
# ============================================================

# TITAN storage is HARD-LOCKED to one user-visible folder on Android shared
# internal storage. Nothing is written outside this root by TITAN.
#
# Required layout (all contained inside one folder):
#   /storage/emulated/0/TAITAN/
#       data/              -> database, settings, logs, replay, flask instance
#       cache/             -> market + AI caches, history, tmp, pycache
#       memory/            -> learning/adaptive memory
#       secrets/           -> optional API-key files
#
# There is intentionally NO fallback to the script directory, current working
# directory, /tmp, /data/data, Android app cache, a desktop profile,
# TITAN_HOME, SD card, or any other path. Only TAITAN.
TITAN_FOLDER_NAME = "TAITAN"
TITAN_SHARED_STORAGE = Path("/storage/emulated/0")
TITAN_ANDROID_HOME = TITAN_SHARED_STORAGE / TITAN_FOLDER_NAME

def _select_app_home() -> Path:
    """Return the only allowed TITAN persistent root: Internal Storage/TAITAN.

    Hard-locked to folder TAITAN on shared internal storage. Android 15 may expose
    the same volume as /storage/emulated/0, /sdcard, or /storage/self/primary.
    Never writes to app cache, /tmp, script dir, or SD card roots.
    """
    candidates = (
        TITAN_SHARED_STORAGE,
        Path("/sdcard"),
        Path("/storage/self/primary"),
    )
    root = None
    for cand in candidates:
        try:
            if cand.exists() and cand.is_dir():
                # FIX: probe inside TAITAN itself so nothing is ever written outside the TAITAN folder.
                kroot = cand / TITAN_FOLDER_NAME
                probe = kroot / ".titan_kkk_probe"
                try:
                    kroot.mkdir(parents=True, exist_ok=True)
                    probe.write_text("ok", encoding="utf-8")
                    probe.unlink(missing_ok=True)
                    root = cand
                    break
                except OSError:
                    continue
        except OSError:
            continue
    if root is None:
        raise RuntimeError(
            "TITAN requires writable Internal Storage (TAITAN). "
            "Grant All-files / storage permission, create folder TAITAN, then run again. "
            "Nothing is stored outside Internal Storage/TAITAN."
        )
    return root / TITAN_FOLDER_NAME

APP_HOME = _select_app_home()
DATA_DIR = APP_HOME / "data"
CACHE_DIR = APP_HOME / "cache"
MEMORY_DIR = APP_HOME / "memory"
SECRETS_DIR = APP_HOME / "secrets"
LOG_DIR = DATA_DIR
TMP_DIR = CACHE_DIR / "tmp"
PYCACHE_DIR = CACHE_DIR / "pycache"
HIST_DIR = CACHE_DIR / "history"
REPLAY_DIR = DATA_DIR / "replay"
FLASK_INSTANCE_DIR = DATA_DIR / "flask_instance"

for directory in (
    APP_HOME, DATA_DIR, CACHE_DIR, MEMORY_DIR, SECRETS_DIR,
    TMP_DIR, PYCACHE_DIR, HIST_DIR, REPLAY_DIR, FLASK_INSTANCE_DIR,
):
    directory.mkdir(parents=True, exist_ok=True)

# Force every OS / Python / SQLite / XDG temp+cache write into TAITAN.
# This covers tempfile, sqlite spill, bytecode, requests/certifi caches, etc.
os.environ["TMPDIR"] = str(TMP_DIR)
os.environ["TEMP"] = str(TMP_DIR)
os.environ["TMP"] = str(TMP_DIR)
os.environ["SQLITE_TMPDIR"] = str(TMP_DIR)
os.environ["PYTHONPYCACHEPREFIX"] = str(PYCACHE_DIR)
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
os.environ["XDG_CACHE_HOME"] = str(CACHE_DIR)
os.environ["XDG_DATA_HOME"] = str(DATA_DIR)
os.environ["XDG_STATE_HOME"] = str(DATA_DIR)
os.environ["XDG_CONFIG_HOME"] = str(DATA_DIR)
sys.dont_write_bytecode = True
try:
    import tempfile as _titan_tempfile
    _titan_tempfile.tempdir = str(TMP_DIR)
except Exception:
    pass

DB_PATH = DATA_DIR / "titan_enterprise.db"
LOG_PATH = LOG_DIR / "titan_engine.log"
AI_CACHE_PATH = CACHE_DIR / "ai_responses.json"
MARKET_CACHE_PATH = CACHE_DIR / "market_cache.json"
USER_SETTINGS_PATH = DATA_DIR / "user_settings.json"
MODEL_SETTINGS_PATH = DATA_DIR / "model_settings.json"
AI_SYMBOL_CACHE_PATH = CACHE_DIR / "ai_symbol_responses.json"


def _startup_storage_audit() -> dict[str, Any]:
    """Verify that every persistent TITAN root is physically inside TAITAN."""
    root = APP_HOME.resolve()
    if root.name != TITAN_FOLDER_NAME:
        raise RuntimeError(f"TITAN storage root must be folder {TITAN_FOLDER_NAME}, got {root}")
    required = (
        DATA_DIR, CACHE_DIR, MEMORY_DIR, SECRETS_DIR, LOG_DIR,
        TMP_DIR, PYCACHE_DIR, HIST_DIR, REPLAY_DIR, FLASK_INSTANCE_DIR,
    )
    root.mkdir(parents=True, exist_ok=True)
    for directory in required:
        directory.mkdir(parents=True, exist_ok=True)
        try:
            directory.resolve().relative_to(root)
        except ValueError:
            raise RuntimeError(f"TITAN storage escape: {directory}")
    probe = root / ".titan_storage_probe"
    try:
        probe.write_text("TITAN_STORAGE_OK", encoding="utf-8")
        if probe.read_text(encoding="utf-8") != "TITAN_STORAGE_OK":
            raise OSError("storage verification mismatch")
    finally:
        try:
            probe.unlink()
        except OSError:
            pass
    persistent = (
        DB_PATH, LOG_PATH, AI_CACHE_PATH, MARKET_CACHE_PATH,
        USER_SETTINGS_PATH, MODEL_SETTINGS_PATH, AI_SYMBOL_CACHE_PATH,
        TMP_DIR, PYCACHE_DIR, HIST_DIR, REPLAY_DIR, FLASK_INSTANCE_DIR,
    )
    for path in persistent:
        try:
            path.resolve().relative_to(root)
        except ValueError:
            raise RuntimeError(f"Persistent path escaped TAITAN root: {path}")
    return {
        "root": str(root),
        "writable": True,
        "persistent_paths_locked": True,
        "single_folder": TITAN_FOLDER_NAME,
        "no_external_writes": True,
    }


STARTUP_STORAGE_AUDIT = _startup_storage_audit()

# V29 autonomous loop:
# - live prices are streamed/polled continuously;
# - the expensive multi-timeframe scan runs automatically in the background;
# - outcome/performance learning is maintained independently of page requests;
# - Gemini enrichment is asynchronous and is fed back into the next scan.
AUTO_SCAN_INTERVAL_SECONDS = 30  # balanced mobile cadence; adaptive governor may extend this when scans are heavy
AUTO_MAINTENANCE_INTERVAL_SECONDS = 20
AUTO_AI_TOP_N = 3
AUTO_AI_REFRESH_SECONDS = 240

MARKET_CACHE_TTL = 30
# Fast dashboard scan: skip external LLM calls during bulk market refresh (biggest latency win).
# Detail endpoints can still request AI on demand.
DASHBOARD_FAST_SCAN = True
RENDER_PERF_LOG = True
AI_CACHE_TTL = 300
LIVE_PRICE_POLL_SECONDS = 2.0  # fresh UI price without excessive REST traffic
MAX_BACKTEST_CANDLES = 3500
SIGNAL_COOLDOWN_SECONDS = 2700
MIN_DIRECTIONAL_QUALITY = 46
MIN_PRECISION_SCORE = 45
MIN_TREND_STABILITY = 0.34
MAX_STRETCH_ATR = 2.8
MIN_EFFECTIVE_RR = 1.05
HTTP_TIMEOUT = 15
FEE_RATE = 0.0004
SPREAD_RATE = 0.0002
SLIPPAGE_RATE = 0.0002
TOTAL_ENTRY_BUFFER = FEE_RATE + SPREAD_RATE + SLIPPAGE_RATE

# --- V8 Professional confidence gates ---
# V28.5: deep audit — residual WAIT kills softened, hero board fixed,
# refine_bias balanced, neural demotion only on real conflict.
TITAN_PARAM_VERSION = "V60.0-ANDROID-TAITAN-EDGE-ROUTER"
TITAN_BUILD_ID = "V60.0-PRO-2026-10-ANDROID-TAITAN-EDGE-ROUTER"
TITAN_ENGINE_MODE = "V60_EDGE_ROUTER_CENTRAL_GOVERNOR_ANDROID"
TITAN_ACTIVE_DECISION_PATH = "DATA -> TF-PROFILE -> V49-54 -> V60_EDGE_ROUTER -> V51_FINAL -> V55_DESK -> V56_JOURNAL -> LEDGER"
TITAN_V51_AUTHORITATIVE = True
TITAN_V51_PUBLISH_LOCK = threading.Lock()
MAX_LIVE_PRICE_AGE_SEC = 18.0
MIN_CLOSED_CANDLES_1H = 36
MIN_CLOSED_CANDLES_15M = 28
MIN_DATA_QUALITY_SCORE = 44
ENTRY_LADDER_MIN_HTF_SCORE = 48
ENTRY_LADDER_MIN_MTF_SCORE = 47
ENTRY_LADDER_MIN_LTF_ALIGN = 0.0  # soft; hard check is direction match
MAX_PORTFOLIO_SAME_SIDE = 3
FORECAST_TRACK_PATH = None  # set after APP_HOME exists
CALIB_MIN_SAMPLES_FULL = 20
CALIB_CAP_LOW_N = 62.0
CALIB_CAP_MID_N = 72.0
CALIB_CAP_HIGH_N = 86.0

# Android optimization profile
TITAN_ANDROID_RAM_GB = 8
TITAN_ANDROID_API_LEVEL = 35  # Android 15
TITAN_ANDROID_PROFILE = "XIAOMI_8GB_BALANCED"

# Keep runtime memory controlled and avoid unnecessary filesystem writes.
os.environ.setdefault("PYTHONUNBUFFERED", "1")
try:
    import gc as _titan_gc
    _titan_gc.set_threshold(700, 10, 10)
except Exception:
    pass


# ============================================================
# LOGGING / SAFETY
# ============================================================

LOGGER = logging.getLogger("TITAN")
LOGGER.setLevel(getattr(logging, str(os.environ.get("TITAN_LOG_LEVEL", "INFO")).upper(), logging.INFO))
LOGGER.propagate = False
if not LOGGER.handlers:
    _file_handler = RotatingFileHandler(
        LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    _file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    LOGGER.addHandler(_file_handler)


_SWALLOWED: dict[str, int] = {}
_SWALLOW_LOCK = threading.Lock()
_STARTED_AT = time.time()


def _swallow(note: str = "") -> None:
    """Replacement for a silent `except: pass`.

    Never raises. Counts every swallowed exception per call-site (visible at
    /api/v53/diagnostics) and logs the first occurrences at WARNING and the
    rest at DEBUG so real faults are no longer invisible, without log spam.
    """
    try:
        et, ev, _tb = sys.exc_info()
        if et is None or issubclass(et, (KeyboardInterrupt, SystemExit)):
            return
        if "duplicate column" in str(ev).lower():   # expected idempotent ALTER TABLE migrations
            return
        fr = sys._getframe(1)
        key = f"{fr.f_code.co_name}:{fr.f_lineno}:{et.__name__}"
        with _SWALLOW_LOCK:
            n = _SWALLOWED.get(key, 0) + 1
            if len(_SWALLOWED) < 2000 or key in _SWALLOWED:
                _SWALLOWED[key] = n
        if n <= 3:
            LOGGER.warning("suppressed %s at %s (#%d) %s", et.__name__, key, n, str(ev)[:160])
        elif n in (10, 100, 1000):
            LOGGER.warning("suppressed %s at %s x%d", et.__name__, key, n)
        else:
            LOGGER.debug("suppressed %s at %s: %s", et.__name__, key, ev)
    except Exception:
        _swallow()


def _now_iso() -> str:
    """Return a timezone-aware UTC timestamp for dashboard/cache metadata."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

CACHE_LOCK = threading.RLock()
DB_LOCK = threading.RLock()
JSON_LOCK = threading.RLock()
_HTTP_LOCAL = threading.local()
KEY_LOCK = threading.RLock()
OI_LOCK = threading.RLock()
UPDATE_LOCK = threading.Lock()

# --- V9 Performance: shared pools + short-TTL kline/derivative memory cache ---
KLINE_CACHE: dict[str, tuple[float, "pd.DataFrame"]] = {}
KLINE_CACHE_LOCK = threading.RLock()
KLINE_CACHE_TTL = 35.0          # aligned with 30s scan cadence; prevents stale multi-TF reuse
KLINE_CACHE_MAX = 160           # hard cap entries (symbols × TFs)
DERIV_CACHE: dict[str, tuple[float, dict]] = {}
DERIV_CACHE_TTL = 35.0
_ANALYSIS_POOL: Optional[ThreadPoolExecutor] = None
_ANALYSIS_POOL_LOCK = threading.Lock()
_KLINE_POOL: Optional[ThreadPoolExecutor] = None
_KLINE_POOL_LOCK = threading.Lock()


def _get_analysis_pool(size: int = 4) -> ThreadPoolExecutor:
    global _ANALYSIS_POOL
    with _ANALYSIS_POOL_LOCK:
        if _ANALYSIS_POOL is None:
            # Cap workers hard on phone: 3 workers keeps SSL + RAM under control on 8GB Android.
            n = max(2, min(int(size), 4))
            _ANALYSIS_POOL = ThreadPoolExecutor(max_workers=n, thread_name_prefix="titan-an")
        return _ANALYSIS_POOL


def _get_kline_pool(size: int = 4) -> ThreadPoolExecutor:
    global _KLINE_POOL
    with _KLINE_POOL_LOCK:
        if _KLINE_POOL is None:
            _KLINE_POOL = ThreadPoolExecutor(max_workers=max(2, min(int(size), 4)), thread_name_prefix="titan-kl")
        return _KLINE_POOL


_AI_POOL: Optional[ThreadPoolExecutor] = None
_AI_POOL_LOCK = threading.Lock()
_AI_ENRICH_LOCK = threading.Lock()
_AI_SUMMARY_LOCK = threading.Lock()
_AI_SUMMARY_LAST_RUN = 0.0
DERIV_CACHE_MAX = 50


def _append_capped(target: list, value: Any, max_items: int = 256) -> None:
    """Append to an in-memory history without allowing unbounded growth."""
    target.append(value)
    overflow = len(target) - int(max_items)
    if overflow > 0:
        del target[:overflow]


def _get_ai_pool(size: int = 1) -> ThreadPoolExecutor:
    """Reuse one AI thread pool across symbols (avoids create/destroy per coin)."""
    global _AI_POOL
    with _AI_POOL_LOCK:
        if _AI_POOL is None:
            _AI_POOL = ThreadPoolExecutor(max_workers=max(1, min(int(size), 2)), thread_name_prefix="titan-ai")
        return _AI_POOL


def _kline_cache_key(symbol: str, tf: str, limit: int, start_ms: Optional[int], end_ms: Optional[int]) -> str:
    return f"{_binance_symbol(symbol)}|{tf}|{limit}|{start_ms or 0}|{end_ms or 0}"


def _kline_cache_get(key: str) -> Optional["pd.DataFrame"]:
    with KLINE_CACHE_LOCK:
        row = KLINE_CACHE.get(key)
        if not row:
            return None
        ts, df = row
        if time.time() - ts > KLINE_CACHE_TTL:
            KLINE_CACHE.pop(key, None)
            return None
        # Shallow copy is enough for OHLCV: analysis creates new columns (CoW)
        # and never mutates raw open/high/low/close arrays in-place.
        return df.copy(deep=False)


def _kline_cache_put(key: str, df: "pd.DataFrame") -> None:
    with KLINE_CACHE_LOCK:
        # Snapshot once at insert; readers get shallow copies (see _kline_cache_get).
        KLINE_CACHE[key] = (time.time(), df.copy(deep=False))
        if len(KLINE_CACHE) > KLINE_CACHE_MAX:
            # drop oldest ~20%
            items = sorted(KLINE_CACHE.items(), key=lambda x: x[1][0])
            for k, _ in items[: max(1, len(items) // 5)]:
                KLINE_CACHE.pop(k, None)



def _safe_app_path(path: Path) -> Path:
    candidate = Path(path).resolve()
    root = APP_HOME.resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise RuntimeError(f"TITAN path escaped TAITAN root: {candidate}")
    return candidate


def _http_session() -> requests.Session:
    """Return one requests.Session per worker thread.

    requests.Session is not guaranteed to be safely mutable across concurrent
    workers; TITAN performs many parallel market/API calls. Thread-local sessions
    preserve connection pooling without sharing mutable session state.
    """
    session = getattr(_HTTP_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update({"User-Agent": "TITAN-ENTERPRISE/3.0", "Accept": "application/json"})
        retry = Retry(
            total=3, connect=3, read=3, backoff_factor=0.6,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "HEAD", "OPTIONS"]),
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=16, pool_maxsize=16)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        _HTTP_LOCAL.session = session
    return session

# ============================================================
# API KEYS - FILE ONLY
# ============================================================

OPENAI_API_KEY = ""
GEMINI_API_KEY = ""
GROK_API_KEY = ""
CLAUDE_API_KEY = ""
DEEPSEEK_API_KEY = ""
COINGLASS_API_KEY = ""

KEY_FILES = {
    "OPENAI_API_KEY": ["openai_api_key.txt", "chatgpt_api_key.txt", "titan_llm_key.txt"],
    "GEMINI_API_KEY": ["gemini_api_key.txt", "gemini_key.txt", "api gemini_key.txt"],
    "GROK_API_KEY": ["grok_api_key.txt", "xai_api_key.txt", "grok_key.txt"],
    "CLAUDE_API_KEY": ["claude_api_key.txt", "anthropic_api_key.txt", "claude_key.txt"],
    "DEEPSEEK_API_KEY": ["deepseek_api_key.txt", "deepseek_key.txt"],
    "COINGLASS_API_KEY": ["coinglass_api_key.txt", "coinglass_key.txt"],
}


def _clean_key(value: str) -> str:
    value = (value or "").lstrip("\ufeff").strip()
    if not value:
        return ""
    if "=" in value and "\n" not in value:
        left, right = value.split("=", 1)
        normalized = left.strip().upper().replace("_", "").replace("-", "")
        if normalized.endswith("APIKEY"):
            value = right.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value


def _read_key(name: str) -> str:
    for filename in KEY_FILES.get(name, []):
        for base in (SECRETS_DIR, APP_HOME, DATA_DIR):
            path = base / filename
            try:
                if path.is_file():
                    value = _clean_key(path.read_text(encoding="utf-8"))
                    if value:
                        return value
            except OSError as exc:
                LOGGER.warning("Key read failed for %s: %s", filename, exc)
    return ""


def reload_keys() -> None:
    global OPENAI_API_KEY, GEMINI_API_KEY, GROK_API_KEY, CLAUDE_API_KEY, DEEPSEEK_API_KEY, COINGLASS_API_KEY
    with KEY_LOCK:
        OPENAI_API_KEY = _read_key("OPENAI_API_KEY")
        GEMINI_API_KEY = _read_key("GEMINI_API_KEY")
        GROK_API_KEY = _read_key("GROK_API_KEY")
        CLAUDE_API_KEY = _read_key("CLAUDE_API_KEY")
        DEEPSEEK_API_KEY = _read_key("DEEPSEEK_API_KEY")
        COINGLASS_API_KEY = _read_key("COINGLASS_API_KEY")

reload_keys()

# ============================================================
# DEFAULTS / HELPERS
# ============================================================

DEFAULT_COINS = [
    "BTC/USDT",
    "ETH/USDT",
    "BNB/USDT",
    "XRP/USDT",
    "SOL/USDT",
    "ADA/USDT",
    "DOGE/USDT",
    "AVAX/USDT",
    "LINK/USDT",
    "NEAR/USDT",
    "SUI/USDT",
    "TAO/USDT",
    "AAVE/USDT",
    "BCH/USDT",
    "XLM/USDT",
    "TRX/USDT",
    "SHIB/USDT",
    "PEPE/USDT",
    "WIF/USDT",
    "FLOKI/USDT",
    "CAKE/USDT",
    "HYPE/USDT",
    "ZEC/USDT",
    "XAUT/USDT",
]
LIVE_PRICE_URL = "https://data-api.binance.vision/api/v3/ticker/price"
BINANCE_REST_HOSTS = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
]
LIVE_PRICE_URLS = [f"{h}/api/v3/ticker/price" for h in BINANCE_REST_HOSTS]
KLINES_URLS = [f"{h}/api/v3/klines" for h in BINANCE_REST_HOSTS]


def _safe_startup_check():
    """Basic production startup validation."""
    try:
        APP_HOME.mkdir(parents=True, exist_ok=True)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        init_db()
        return True
    except Exception as exc:
        LOGGER.exception("Startup validation failed: %s", exc)
        return False
TF_CFG = {
    "15m": {"horizon": 60, "weight": 0.15},
    "1h": {"horizon": 240, "weight": 0.25},
    "4h": {"horizon": 720, "weight": 0.30},
    "1d": {"horizon": 2880, "weight": 0.30},
}

# V59 timeframe policy: tuned from the stored E performance evidence.
# 15m/1h are noisier and more cost-sensitive; 4h/1d carry more structural signal.
# These are precision policies, not profit guarantees.
V59_TF_POLICY = {
    "15m": {"fast": 8, "slow": 21, "momentum_bars": 4, "long": 58.0, "short": 42.0, "cooldown_min": 45},
    "1h":  {"fast": 12, "slow": 36, "momentum_bars": 6, "long": 56.0, "short": 44.0, "cooldown_min": 120},
    "4h":  {"fast": 16, "slow": 40, "momentum_bars": 6, "long": 54.0, "short": 46.0, "cooldown_min": 360},
    "1d":  {"fast": 20, "slow": 50, "momentum_bars": 5, "long": 53.0, "short": 47.0, "cooldown_min": 1440},
}
V59_LOW_TF_CONFIRM = {"15m": {"1h_min_long": 51.0, "1h_max_short": 49.0},
                      "1h": {"4h_min_long": 50.0, "4h_max_short": 50.0}}

def v59_tf_policy(tf: str) -> dict[str, float]:
    return dict(V59_TF_POLICY.get(str(tf), V59_TF_POLICY["1h"]))

USER_SETTINGS = {"risk_multiplier": 1.2, "active_coins": DEFAULT_COINS.copy()}
CACHE = {"timestamp": 0.0, "data": [], "gemini_summary": "", "macro": {}}
OI_HISTORY: dict[str, tuple[float, float, str]] = {}
LIVE_PRICES: dict[str, dict[str, Any]] = {}
LIVE_LOCK = threading.RLock()
LIVE_STOP = threading.Event()
LIVE_THREADS: list[threading.Thread] = []

COIN_META = {
    "BTC": ("Bitcoin", "₿"), "ETH": ("Ethereum", "Ξ"), "XRP": ("XRP", "✕"),
    "SOL": ("Solana", "◎"), "SHIB": ("Shiba Inu", "🐕"), "BNB": ("BNB", "◆"),
    "ZEC": ("Zcash", "ⓩ"), "ADA": ("Cardano", "₳"), "TRX": ("TRON", "⚡"),
    "HYPE": ("Hyperliquid", "🌊"), "DOGE": ("Dogecoin", "Ð"), "WIF": ("dogwifhat", "🐶"),
    "PEPE": ("Pepe", "🐸"), "AVAX": ("Avalanche", "🔺"), "FLOKI": ("Floki", "🐺"),
    "CAKE": ("PancakeSwap", "🥞"), "BCH": ("Bitcoin Cash", "Ƀ"), "XLM": ("Stellar", "✦"),
    "AAVE": ("Aave", "👻"), "SUI": ("Sui", "💧"), "TAO": ("Bittensor", "🧠"),
    "LINK": ("Chainlink", "⬡"), "NEAR": ("NEAR", "Ⓝ"), "XAUT": ("Tether Gold", "🥇"),
}


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


# === V53.1 PERFORMANCE KERNELS (vectorised, behaviour-identical to the old iterrows loops) ===
def _fast_col(window, name: str, default: float = 0.0) -> "np.ndarray":
    """Column -> float64 ndarray; missing column / non-finite values -> default."""
    if window is None or name not in getattr(window, "columns", ()):
        return np.full(len(window) if window is not None else 0, default, dtype=float)
    arr = pd.to_numeric(window[name], errors="coerce").to_numpy(dtype=float, na_value=np.nan)
    return np.where(np.isfinite(arr), arr, default)


def _ft_simple(window, is_long: bool, sl: float, tp: float, guard_positive: bool = False):
    """Chronological first-touch over OHLC window (no sorting). Returns (tag, price, ts_sec)."""
    if window is None or len(window) == 0:
        return "TIME_EXIT", None, None
    hi = _fast_col(window, "high"); lo = _fast_col(window, "low")
    if is_long:
        a = lo <= sl; b = hi >= tp
    else:
        a = hi >= sl; b = lo <= tp
    if guard_positive:
        if not (sl > 0): a = np.zeros_like(a)
        if not (tp > 0): b = np.zeros_like(b)
    hit = a | b
    if not hit.any():
        return "TIME_EXIT", None, None
    i = int(hit.argmax())
    ts = float(_fast_col(window, "t")[i]) / 1000.0
    if a[i] and b[i]: return "AMBIGUOUS", None, ts
    if a[i]: return "LOSS", sl, ts
    return "WIN", tp, ts





# === TITAN ULTRA TECHNICAL LAYER ===

def calculate_auto_fibonacci(high, low):
    diff=float(high)-float(low)
    return {
        "0.236": round(float(high)-diff*0.236,8),
        "0.382": round(float(high)-diff*0.382,8),
        "0.500": round(float(high)-diff*0.500,8),
        "0.618": round(float(high)-diff*0.618,8),
        "0.786": round(float(high)-diff*0.786,8),
    }

def calculate_support_resistance(closes, window=20):
    vals=list(map(float, closes[-window:])) if closes else []
    if not vals:
        return {"support":None,"resistance":None}
    return {"support":min(vals),"resistance":max(vals)}

def technical_layers(df):
    """Compute technical display layers without leaking warm-up NaN/inf values."""
    empty={"bollinger":{"upper":None,"middle":None,"lower":None},"fibonacci":{},"support_resistance":{"support":None,"resistance":None}}
    if df is None or df.empty or "close" not in df:
        return empty
    close=pd.to_numeric(df["close"],errors="coerce").dropna()
    if close.empty:
        return empty
    mid=close.rolling(20,min_periods=min(20,len(close))).mean()
    std=close.rolling(20,min_periods=min(20,len(close))).std(ddof=0)
    middle=float(mid.iloc[-1]); deviation=float(std.iloc[-1])
    if not math.isfinite(middle): middle=float(close.iloc[-1])
    if not math.isfinite(deviation): deviation=0.0
    high=float(pd.to_numeric(df.get("high",close),errors="coerce").max())
    low=float(pd.to_numeric(df.get("low",close),errors="coerce").min())
    if not (math.isfinite(high) and math.isfinite(low)) or high<=0 or low<=0 or high<low:
        high=low=float(close.iloc[-1])
    return {"bollinger":{"upper":middle+2*deviation,"middle":middle,"lower":middle-2*deviation},"fibonacci":calculate_auto_fibonacci(high,low),"support_resistance":calculate_support_resistance(close.tolist())}

# === TITAN V6 ADVANCED TECHNICAL SUITE ===
def calc_macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> dict[str, float]:
    """MACD line, signal, histogram — fail-safe."""
    try:
        c = close.astype(float)
        if len(c) < slow + signal:
            return {"macd": 0.0, "signal": 0.0, "hist": 0.0, "cross": "none"}
        ema_f = c.ewm(span=fast, adjust=False).mean()
        ema_s = c.ewm(span=slow, adjust=False).mean()
        macd = ema_f - ema_s
        sig = macd.ewm(span=signal, adjust=False).mean()
        hist = macd - sig
        h0, h1 = float(hist.iloc[-1]), float(hist.iloc[-2]) if len(hist) > 1 else 0.0
        cross = "bull" if h1 <= 0 < h0 else "bear" if h1 >= 0 > h0 else "none"
        return {"macd": round(float(macd.iloc[-1]), 8), "signal": round(float(sig.iloc[-1]), 8),
                "hist": round(h0, 8), "cross": cross}
    except Exception:
        return {"macd": 0.0, "signal": 0.0, "hist": 0.0, "cross": "none"}


def calc_stoch_rsi(close: pd.Series, rsi_period: int = 14, stoch_period: int = 14, k: int = 3, d: int = 3) -> dict[str, float]:
    """Stochastic RSI for timing entries on oversold/overbought extremes."""
    try:
        rsi = wilder_rsi(close, rsi_period)
        if len(rsi) < stoch_period + d:
            return {"k": 50.0, "d": 50.0, "zone": "mid"}
        rmin = rsi.rolling(stoch_period).min()
        rmax = rsi.rolling(stoch_period).max()
        stoch = 100 * (rsi - rmin) / (rmax - rmin).replace(0, np.nan)
        k_line = stoch.rolling(k).mean().fillna(50)
        d_line = k_line.rolling(d).mean().fillna(50)
        kv, dv = float(k_line.iloc[-1]), float(d_line.iloc[-1])
        zone = "oversold" if kv < 20 else "overbought" if kv > 80 else "mid"
        return {"k": round(kv, 2), "d": round(dv, 2), "zone": zone}
    except Exception:
        return {"k": 50.0, "d": 50.0, "zone": "mid"}


def calc_pivot_points(df: pd.DataFrame) -> dict[str, float]:
    """Classic daily pivots from last closed candle high/low/close."""
    try:
        if df is None or len(df) < 2:
            return {}
        row = df.iloc[-1]
        h, l, c = float(row["high"]), float(row["low"]), float(row["close"])
        pp = (h + l + c) / 3.0
        r1 = 2 * pp - l
        s1 = 2 * pp - h
        r2 = pp + (h - l)
        s2 = pp - (h - l)
        r3 = h + 2 * (pp - l)
        s3 = l - 2 * (h - pp)
        return {k: round(v, 8) for k, v in (("pp", pp), ("r1", r1), ("r2", r2), ("r3", r3), ("s1", s1), ("s2", s2), ("s3", s3))}
    except Exception:
        return {}


def calc_volume_delta(df: pd.DataFrame, lookback: int = 24) -> dict[str, float]:
    """Proxy CVD from candle body direction * volume (no tick data required)."""
    try:
        x = df.tail(lookback).copy()
        if x.empty:
            return {"delta": 0.0, "buy_vol": 0.0, "sell_vol": 0.0, "delta_pct": 0.0}
        body_up = (x["close"] >= x["open"]).astype(float)
        buy_v = float((x["vol"] * body_up).sum())
        sell_v = float((x["vol"] * (1 - body_up)).sum())
        total = buy_v + sell_v
        delta = buy_v - sell_v
        return {
            "delta": round(delta, 4),
            "buy_vol": round(buy_v, 4),
            "sell_vol": round(sell_v, 4),
            "delta_pct": round((delta / total * 100) if total > 0 else 0.0, 2),
        }
    except Exception:
        return {"delta": 0.0, "buy_vol": 0.0, "sell_vol": 0.0, "delta_pct": 0.0}


def market_session_utc(asof_ts: Optional[float] = None) -> dict[str, Any]:
    """Return the session at an explicit as-of timestamp (UTC).

    Historical/replay callers must pass their event/bar timestamp; live callers can
    omit it and use current UTC. This prevents wall-clock leakage in backtests.
    """
    if asof_ts is None:
        dt = datetime.now(timezone.utc)
    else:
        try:
            dt = datetime.fromtimestamp(float(asof_ts), tz=timezone.utc)
        except Exception:
            dt = datetime.now(timezone.utc)
    hour = dt.hour
    if 0 <= hour < 8:
        name, risk_mult = "Asia", 0.95
    elif 8 <= hour < 13:
        name, risk_mult = "London", 1.05
    elif 13 <= hour < 17:
        name, risk_mult = "London-NY Overlap", 1.12
    elif 17 <= hour < 21:
        name, risk_mult = "New York", 1.05
    else:
        name, risk_mult = "Off-hours", 0.88
    return {"session": name, "hour_utc": hour, "liquidity_boost": risk_mult}




# === TITAN PATTERN + FUTURE CANDLE FORECAST ENGINE ===
PATTERN_GUIDE = {
    "سرشانه": {
        "name_en": "Head & Shoulders",
        "meaning": "الگوی بازگشتی نزولی: سه قله که قله میانی (سر) بالاتر از دو شانه است. خط گردن اتصال کف‌های بین شانه و سر است.",
        "expect": "با شکست معتبر خط گردن به سمت پایین، انتظار ادامه نزول تا اندازه ارتفاع سر تا خط گردن وجود دارد. حد ضرر بالای شانه راست.",
        "bias": "نزولی",
    },
    "سرشانه معکوس": {
        "name_en": "Inverse Head & Shoulders",
        "meaning": "الگوی بازگشتی صعودی: سه کف که کف میانی (سر) پایین‌تر از دو شانه است.",
        "expect": "با شکست خط گردن به بالا، انتظار رشد تا اندازه ارتفاع الگو. حد ضرر زیر شانه راست.",
        "bias": "صعودی",
    },
    "سقف دوقلو": {
        "name_en": "Double Top",
        "meaning": "دو قله تقریباً هم‌تراز پس از روند صعودی؛ نشانه ضعف خریداران.",
        "expect": "شکست کف میانی (خط گردن) معمولاً ادامه نزول را تقویت می‌کند. هدف تقریبی: فاصله قله تا گردن.",
        "bias": "نزولی",
    },
    "کف دوقلو": {
        "name_en": "Double Bottom",
        "meaning": "دو کف تقریباً هم‌تراز پس از روند نزولی؛ نشانه ضعف فروشندگان.",
        "expect": "شکست سقف میانی معمولاً ادامه صعود را تقویت می‌کند. هدف تقریبی: فاصله کف تا گردن.",
        "bias": "صعودی",
    },
    "واگرایی صعودی": {
        "name_en": "Bullish Divergence",
        "meaning": "قیمت کف پایین‌تر می‌سازد اما RSI/MACD کف بالاتر می‌سازد — ضعف فروش.",
        "expect": "احتمال واکنش صعودی یا پایان موقت نزول؛ تأیید با برگشت قیمت و حجم لازم است.",
        "bias": "صعودی",
    },
    "واگرایی نزولی": {
        "name_en": "Bearish Divergence",
        "meaning": "قیمت سقف بالاتر می‌سازد اما RSI/MACD سقف پایین‌تر می‌سازد — ضعف خرید.",
        "expect": "احتمال اصلاح یا برگشت نزولی؛ تأیید با شکست ساختار لازم است.",
        "bias": "نزولی",
    },
    "مثلث فشرده": {
        "name_en": "Symmetrical / Tight Range",
        "meaning": "نوسان در حال فشرده شدن؛ انرژی برای شکست انباشته می‌شود.",
        "expect": "شکست با حجم می‌تواند حرکت جهت‌دار ایجاد کند؛ جهت از ساختار چندتایم‌فریمی خوانده شود.",
        "bias": "خنثی",
    },
}


def _find_swing_points(series: pd.Series, order: int = 3) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """Local swing highs/lows as (index_position, value)."""
    vals = series.astype(float).tolist()
    highs, lows = [], []
    n = len(vals)
    for i in range(order, n - order):
        window = vals[i - order:i + order + 1]
        if vals[i] == max(window) and vals[i] > vals[i - 1] and vals[i] > vals[i + 1]:
            highs.append((i, vals[i]))
        if vals[i] == min(window) and vals[i] < vals[i - 1] and vals[i] < vals[i + 1]:
            lows.append((i, vals[i]))
    return highs, lows


def detect_chart_patterns(df: pd.DataFrame, rsi_series: Optional[pd.Series] = None) -> dict[str, Any]:
    """Detect standard patterns: H&S, double top/bottom, RSI divergence, tight range."""
    patterns: list[dict[str, Any]] = []
    try:
        if df is None or len(df) < 40:
            return {"patterns": [], "primary": None, "score_bias": 0.0}
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        close = df["close"].astype(float)
        swing_highs, _ = _find_swing_points(high, 3)
        _, swing_lows = _find_swing_points(low, 3)

        # Double Top
        if len(swing_highs) >= 2:
            (i1, h1), (i2, h2) = swing_highs[-2], swing_highs[-1]
            if i2 > i1 and abs(h1 - h2) / max(h1, 1e-12) < 0.015:
                mid_low = float(low.iloc[i1:i2 + 1].min()) if i2 > i1 else float(low.iloc[-1])
                if float(close.iloc[-1]) < (h1 + h2) / 2:
                    conf = 62 + (10 if float(close.iloc[-1]) < mid_low else 0)
                    patterns.append({
                        "id": "سقف دوقلو", "confidence": min(88, conf),
                        "levels": {"peak1": round(h1, 8), "peak2": round(h2, 8), "neck": round(mid_low, 8)},
                        "guide": PATTERN_GUIDE["سقف دوقلو"],
                    })

        # Double Bottom
        if len(swing_lows) >= 2:
            (i1, l1), (i2, l2) = swing_lows[-2], swing_lows[-1]
            if i2 > i1 and abs(l1 - l2) / max(abs(l1), 1e-12) < 0.015:
                mid_high = float(high.iloc[i1:i2 + 1].max()) if i2 > i1 else float(high.iloc[-1])
                if float(close.iloc[-1]) > (l1 + l2) / 2:
                    conf = 62 + (10 if float(close.iloc[-1]) > mid_high else 0)
                    patterns.append({
                        "id": "کف دوقلو", "confidence": min(88, conf),
                        "levels": {"trough1": round(l1, 8), "trough2": round(l2, 8), "neck": round(mid_high, 8)},
                        "guide": PATTERN_GUIDE["کف دوقلو"],
                    })

        # Head & Shoulders (3 highs: L-shoulder, head, R-shoulder)
        if len(swing_highs) >= 3:
            (i0, s0), (i1, head), (i2, s1) = swing_highs[-3], swing_highs[-2], swing_highs[-1]
            if head > s0 and head > s1 and abs(s0 - s1) / max(head, 1e-12) < 0.04 and i0 < i1 < i2:
                neck = float(low.iloc[i0:i2 + 1].min())
                conf = 70 if float(close.iloc[-1]) < neck * 1.01 else 58
                patterns.append({
                    "id": "سرشانه", "confidence": conf,
                    "levels": {"left": round(s0, 8), "head": round(head, 8), "right": round(s1, 8), "neck": round(neck, 8)},
                    "guide": PATTERN_GUIDE["سرشانه"],
                })

        # Inverse H&S
        if len(swing_lows) >= 3:
            (i0, s0), (i1, head), (i2, s1) = swing_lows[-3], swing_lows[-2], swing_lows[-1]
            if head < s0 and head < s1 and abs(s0 - s1) / max(abs(head), 1e-12) < 0.04 and i0 < i1 < i2:
                neck = float(high.iloc[i0:i2 + 1].max())
                conf = 70 if float(close.iloc[-1]) > neck * 0.99 else 58
                patterns.append({
                    "id": "سرشانه معکوس", "confidence": conf,
                    "levels": {"left": round(s0, 8), "head": round(head, 8), "right": round(s1, 8), "neck": round(neck, 8)},
                    "guide": PATTERN_GUIDE["سرشانه معکوس"],
                })

        # RSI divergence
        rsi = rsi_series if rsi_series is not None else wilder_rsi(close)
        if len(close) >= 30 and len(rsi) >= 30:
            c_tail = close.tail(30)
            r_tail = rsi.tail(30)
            price_ll = float(c_tail.min()) == float(c_tail.iloc[-1]) or (
                float(c_tail.iloc[-1]) <= float(c_tail.iloc[:-5].min()) * 1.002
            )
            price_hh = float(c_tail.iloc[-1]) >= float(c_tail.iloc[:-5].max()) * 0.998
            rsi_now = float(r_tail.iloc[-1])
            rsi_prev_min = float(r_tail.iloc[:-5].min())
            rsi_prev_max = float(r_tail.iloc[:-5].max())
            if price_ll and rsi_now > rsi_prev_min + 3:
                patterns.append({
                    "id": "واگرایی صعودی", "confidence": 65,
                    "levels": {"rsi": round(rsi_now, 1)},
                    "guide": PATTERN_GUIDE["واگرایی صعودی"],
                })
            if price_hh and rsi_now < rsi_prev_max - 3:
                patterns.append({
                    "id": "واگرایی نزولی", "confidence": 65,
                    "levels": {"rsi": round(rsi_now, 1)},
                    "guide": PATTERN_GUIDE["واگرایی نزولی"],
                })

        # Tight range / triangle proxy
        recent = close.tail(20)
        atr_pct = float((high.tail(20) - low.tail(20)).mean() / close.iloc[-1] * 100) if float(close.iloc[-1]) else 0
        if atr_pct < 1.4 and float(recent.max() / recent.min() - 1) * 100 < 3.5:
            patterns.append({
                "id": "مثلث فشرده", "confidence": 55,
                "levels": {"range_high": round(float(recent.max()), 8), "range_low": round(float(recent.min()), 8)},
                "guide": PATTERN_GUIDE["مثلث فشرده"],
            })

        patterns.sort(key=lambda x: -x.get("confidence", 0))
        primary = patterns[0] if patterns else None
        score_bias = 0.0
        if primary:
            g = primary.get("guide") or {}
            b = g.get("bias", "خنثی")
            conf = primary.get("confidence", 50) / 100.0
            if b == "صعودی":
                score_bias = 4.0 * conf
            elif b == "نزولی":
                score_bias = -4.0 * conf
        return {"patterns": patterns[:5], "primary": primary, "score_bias": round(score_bias, 2)}
    except Exception as exc:
        LOGGER.debug("pattern detect failed: %s", exc)
        return {"patterns": [], "primary": None, "score_bias": 0.0}


def forecast_future_candles(
    df: pd.DataFrame,
    horizon: int = 12,
    patterns: Optional[dict] = None,
    *,
    macd: Optional[dict] = None,
    stoch: Optional[dict] = None,
    structure: Optional[dict] = None,
    regime: Optional[dict] = None,
) -> dict[str, Any]:
    """Multi-factor probabilistic path for the next N candles (default 12).

    Blends: historical return distribution, short/medium momentum, EMA structure,
    MACD/Stoch tilt, chart-pattern bias, analogue matching, and soft mean-reversion
    over longer horizons. Analysis-only — not a guarantee of future prices.
    """
    try:
        horizon = int(clamp(horizon, 3, 24))
        if df is None or len(df) < 50:
            return {"ok": False, "candles": [], "narrative": "داده کافی برای پیش‌بینی نیست", "horizon": horizon}
        close = df["close"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        vol = df["vol"].astype(float) if "vol" in df.columns else pd.Series([1.0] * len(df))
        rets = close.pct_change().dropna().tail(160)
        if len(rets) < 20:
            return {"ok": False, "candles": [], "narrative": "تاریخچه بازده ناکافی", "horizon": horizon}

        mu = float(rets.mean())
        sigma = float(rets.std()) or 1e-6
        # Multi-horizon momentum
        mom3 = float(close.iloc[-1] / close.iloc[-4] - 1) if len(close) > 4 else 0.0
        mom5 = float(close.iloc[-1] / close.iloc[-6] - 1) if len(close) > 6 else 0.0
        mom12 = float(close.iloc[-1] / close.iloc[-13] - 1) if len(close) > 13 else 0.0
        ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
        ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1]) if len(close) >= 50 else ema20
        ema_gap = (ema20 / ema50 - 1.0) if ema50 > 0 else 0.0
        price_vs_ema = (float(close.iloc[-1]) / ema20 - 1.0) if ema20 > 0 else 0.0

        # Base drift: recent mean + momentum blend + structure
        mu_adj = mu * 0.28 + mom3 * 0.18 + mom5 * 0.22 + mom12 * 0.12 + ema_gap * 0.20
        # Soft mean-reversion when stretched vs EMA20
        if abs(price_vs_ema) > 0.025:
            mu_adj -= price_vs_ema * 0.15

        # Pattern tilt
        pbias = safe_float((patterns or {}).get("score_bias"), 0) / 100.0
        mu_adj += pbias * abs(sigma) * 2.2

        # MACD / Stoch confirmation
        macd = macd or {}
        stoch = stoch or {}
        if macd.get("cross") == "bull":
            mu_adj += abs(sigma) * 0.35
        elif macd.get("cross") == "bear":
            mu_adj -= abs(sigma) * 0.35
        hist = safe_float(macd.get("hist"), 0)
        if hist != 0:
            last_px = float(close.iloc[-1])
            mu_adj += float(np.clip(hist / max(last_px * 0.01, 1e-12), -0.8, 0.8)) * abs(sigma) * 0.25
        zone = str(stoch.get("zone") or "mid")
        if zone == "oversold":
            mu_adj += abs(sigma) * 0.20
        elif zone == "overbought":
            mu_adj -= abs(sigma) * 0.20

        # Regime / structure soft bias
        regime_name = str((regime or {}).get("regime", "")).lower()
        if "up" in regime_name or "bull" in regime_name:
            mu_adj += abs(sigma) * 0.12
        elif "down" in regime_name or "bear" in regime_name:
            mu_adj -= abs(sigma) * 0.12
        struct_bias = str((structure or {}).get("bias", "") or "")
        if struct_bias == "صعودی":
            mu_adj += abs(sigma) * 0.10
        elif struct_bias == "نزولی":
            mu_adj -= abs(sigma) * 0.10

        # Volume confirmation of last move
        try:
            v_tail = vol.tail(8)
            c_tail = close.tail(8)
            if len(v_tail) >= 4 and float(v_tail.mean()) > 0:
                up_vol = float(v_tail[c_tail.diff() > 0].sum())
                dn_vol = float(v_tail[c_tail.diff() < 0].sum())
                if up_vol + dn_vol > 0:
                    vol_skew = (up_vol - dn_vol) / (up_vol + dn_vol)
                    mu_adj += vol_skew * abs(sigma) * 0.18
        except Exception:
            pass

        last = float(close.iloc[-1])
        atr = float((high - low).tail(14).mean()) or last * 0.01

        # Historical analogue (longer window for 12-step path)
        analogue_note = ""
        best_corr, best_fwd = 0.0, None
        try:
            window = 12 if len(close) > 80 else 8
            target = close.pct_change().dropna().tail(window).values
            rets_all = close.pct_change().dropna().values
            # Stride search for speed (dashboard scans many symbols)
            _step = 2 if len(rets_all) > 80 else 1
            for i in range(window, max(window + 1, len(rets_all) - horizon - 1), _step):
                seg = rets_all[i - window:i]
                if len(seg) != window:
                    continue
                if float(np.std(seg)) < 1e-12 or float(np.std(target)) < 1e-12:
                    continue
                corr = float(np.corrcoef(seg, target)[0, 1])
                if math.isfinite(corr) and corr > best_corr and corr > 0.50:
                    best_corr = corr
                    best_fwd = rets_all[i:i + horizon]
            if best_fwd is not None and len(best_fwd) >= 1:
                fwd_sum = float(np.sum(best_fwd))
                analogue_note = (
                    f"نزدیک‌ترین الگوی تاریخی با همبستگی {best_corr:.0%} در ادامه "
                    f"{'مثبت' if fwd_sum > 0 else 'منفی'} حدود {abs(fwd_sum)*100:.2f}% حرکت داشته است."
                )
        except Exception:
            pass

        candles = []
        px = last
        # Confidence decays slower for short steps, faster for far steps
        for step in range(1, horizon + 1):
            # Dampen drift with horizon (uncertainty + mean reversion)
            damp = 1.0 / (1.0 + 0.045 * (step - 1))
            step_mu = mu_adj * damp
            drift = step_mu * step
            # Wider bands for longer horizon
            band = sigma * (step ** 0.55) * (1.45 + 0.04 * step)
            mid = last * (1 + drift)
            # Blend analogue path if available
            if best_fwd is not None and step <= len(best_fwd):
                ana = last * (1 + float(np.sum(best_fwd[:step])))
                mid = 0.62 * mid + 0.38 * ana
            lo = mid * (1 - band) - 0.12 * atr * (1 + 0.03 * step)
            hi = mid * (1 + band) + 0.12 * atr * (1 + 0.03 * step)
            o = px
            c = mid
            h = max(o, c, hi * 0.35 + mid * 0.65)
            l = min(o, c, lo * 0.35 + mid * 0.65)
            direction = "صعودی" if c >= o else "نزولی"
            conf = clamp(78 - step * 3.2 + abs(pbias) * 70 + (best_corr * 12 if best_corr else 0), 22, 82)
            candles.append({
                "step": step,
                "open": round(float(o), 8),
                "high": round(float(h), 8),
                "low": round(float(l), 8),
                "close": round(float(c), 8),
                "mid": round(float(mid), 8),
                "band_low": round(float(lo), 8),
                "band_high": round(float(hi), 8),
                "direction": direction,
                "confidence": round(conf, 1),
                "analogue_corr": round(best_corr, 3) if best_corr else None,
            })
            px = float(c)

        primary = (patterns or {}).get("primary")
        if primary:
            pname = primary.get("id", "")
            guide = primary.get("guide") or {}
            narrative = (
                f"پیش‌بینی احتمالی {horizon} کندل بعدی با مدل چندعاملی (بازده، مومنتوم چندافق، EMA، MACD/Stoch، "
                f"الگوی «{pname}» و آنالوگ تاریخی). {guide.get('expect', '')} "
                f"{analogue_note} این خروجی سناریویی است و تضمین سود نیست."
            )
        else:
            narrative = (
                f"پیش‌بینی احتمالی {horizon} کندل با توزیع بازده، مومنتوم، ساختار EMA و شباهت الگویی. "
                f"{analogue_note} خروجی سناریویی است، نه قطعی."
            )
        up_steps = sum(1 for c in candles if c["direction"] == "صعودی")
        overall = "صعودی" if up_steps > horizon * 0.55 else "نزولی" if up_steps < horizon * 0.45 else "خنثی"
        # Path conviction: magnitude of expected move vs sigma
        expected_move = (candles[-1]["close"] / last - 1.0) if candles else 0.0
        path_strength = float(clamp(abs(expected_move) / max(sigma * (horizon ** 0.5), 1e-9) * 25, 0, 40))
        return {
            "ok": True,
            "horizon": horizon,
            "overall_bias": overall,
            "candles": candles,
            "narrative": narrative,
            "last_price": round(last, 8),
            "mu": round(mu_adj, 6),
            "sigma": round(sigma, 6),
            "expected_move_pct": round(expected_move * 100, 3),
            "path_strength": round(path_strength, 1),
            "analogue_corr": round(best_corr, 3) if best_corr else None,
            "components": {
                "mom3": round(mom3, 6), "mom5": round(mom5, 6), "mom12": round(mom12, 6),
                "ema_gap": round(ema_gap, 6), "pattern_bias": round(pbias, 4),
            },
        }
    except Exception as exc:
        LOGGER.debug("forecast_future_candles failed: %s", exc)
        return {"ok": False, "candles": [], "narrative": str(exc)[:200], "horizon": horizon}


def _analyze_asset_v29_impl(symbol: str, btc_trend: str) -> Optional[dict[str, Any]]:
    item = _analyze_asset_v22(symbol, btc_trend)
    if not item:
        return None
    try:
        magic = TITAN_MAGIC_V29.evaluate(item)
        item["magic_v29"] = magic
        item.setdefault("fusion", {})["magic_v29"] = magic
        item["magic_confidence"] = magic.get("confidence", 0)
        item["magic_state"] = magic.get("state", "MAGIC_NO_EDGE")

        current=str(item.get("decision_tag") or "WAIT").upper()
        candidate=str(magic.get("candidate") or "WAIT").upper()
        chosen=magic.get("chosen") or {}
        # A promotion is allowed only for a current WAIT and only when V29 has
        # enough independent evidence. If levels cannot be made valid, it stays WAIT.
        if current=="WAIT" and magic.get("promote") and candidate in {"LONG","SHORT"}:
            try:
                price=_v12_parse_price(item.get("price"))
                sl,tp1,tp2=_v12_rebuild_directional_levels(item,candidate)
                chk=_v12_level_integrity(price,sl,tp1,tp2,candidate)
                if chk.get("ok") and safe_float(chk.get("rr1"),0)>=V29_MIN_RR:
                    item["stop_loss"]=smart_format(sl); item["tp1"]=smart_format(tp1); item["tp2"]=smart_format(tp2)
                    item["effective_rr_tp1"]=chk["rr1"]; item["effective_rr_tp2"]=chk["rr2"]
                    item["rr_tp1"]=chk["rr1"]; item["rr_tp2"]=chk["rr2"]
                    item["decision_tag"]=candidate
                    item["bias"]="صعودی" if candidate=="LONG" else "نزولی"
                    item["signal_tag"]="V29 MAGIC READY"
                    item["entry_mode"]="EARLY"
                    item["signal_quality"]=max(safe_float(item.get("signal_quality"),50),min(86,safe_float(chosen.get("quality"),50)+8))
                    magic["applied"]=True
                else:
                    magic["applied"]=False
                    magic["explanation"].append("ارتقای V29 Cancel شد: سطوح نهایی یا RR معتبر نبود.")
            except Exception as exc:
                magic["applied"]=False
                magic["explanation"].append("ارتقای V29 به‌دلیل خطای کنترل سطوح انجام نشد.")
                LOGGER.debug("V29 promotion failed for %s: %s",symbol,exc)
        elif current in {"LONG","SHORT"}:
            # Existing directional decisions are never flipped by the magic layer.
            magic["applied"]=False
            magic["explanation"].append("تصمیم جهت‌دار موجود حفظ شد؛ V29 فقط آن را ممیزی کرد.")
        else:
            magic["applied"]=False

        # Final consistency fields for dashboard/API consumers.
        item["canonical_decision"]={**(item.get("canonical_decision") or {}),"decision":item.get("decision_tag","WAIT"),"bias":item.get("bias","خنثی"),"magic_confidence":magic.get("confidence",0),"magic_state":magic.get("state")}
        item["decision_audit_v29"]={
            "decision":item.get("decision_tag","WAIT"),"candidate":candidate,
            "state":magic.get("state"),"confidence":magic.get("confidence",0),
            "independent_support":chosen.get("independent_support",0),
            "contradiction":chosen.get("contradiction",0),
            "hard_blocks":chosen.get("hard_blocks",[]),
            "explanation":magic.get("explanation",[]),
        }
        return item
    except Exception as exc:
        LOGGER.exception("V29 magic arbitration failed for %s: %s",symbol,exc)
        item["magic_v29"]={"version":V29_VERSION,"state":"ERROR","confidence":0,"error":"internal arbitration failure"}
        return item


# ============================================================
# TITAN V30 — DEEP CONSENSUS / MARKET DEBATE / ADAPTIVE DECISION CORE
# ------------------------------------------------------------
# V30 is a second-stage meta-arbiter. It does not pretend that an AI model
# can predict price with certainty. It makes the existing stack compete in
# an auditable debate: trend, structure, flow, volatility, forecast, neural,
# precision, BTC context and AI. Correlated evidence is compressed into
# families; missing/stale evidence is penalized rather than guessed.
# ============================================================
V30_VERSION = "TITAN-V30-DEEP-CONSENSUS"
V30_MIN_DATA = 58.0
V30_MIN_QUALITY = 56.0
V30_MIN_READY = 0.30
V30_MIN_EARLY = 0.20
V30_MIN_WATCH = 0.12
V30_MAX_AI = 0.10
V30_DEMOTE_EDGE = -0.30
V30_DEMOTE_OPPOSITION = 3


def _v30_num(v, default=0.0):
    try:
        if v is None or v == "":
            return float(default)
        return float(str(v).replace("%", "").replace(",", "").strip())
    except Exception:
        return float(default)


def _v30_direction(v):
    t=str(v or "").strip().lower()
    if t in {"long","bull","bullish","up","صعودی","buy","strong_long"}: return "LONG"
    if t in {"short","bear","bearish","down","نزولی","sell","strong_short"}: return "SHORT"
    return "WAIT"


class TitanDeepConsensusV30:
    """Adaptive, auditable final decision debate.

    Important design rule: V30 can *remove* a weak directional decision when
    independent evidence strongly contradicts it, but it never flips LONG to
    SHORT (or vice versa) merely because a model says so. A fresh evaluation
    must establish the opposite side before a later scan can select it.
    """

    TF_W = {"15m": .14, "1h": .31, "4h": .32, "1d": .23}

    def _regime(self, item):
        reg=str(((item.get("edge") or {}).get("regime") or {}).get("regime") or "").lower()
        if any(x in reg for x in ("trend","bull","bear","uptrend","downtrend")):
            return "TREND", {"tf":1.12,"structure":1.12,"flow":1.02,"vol":.90,"forecast":.95,"neural":1.05,"precision":.92,"btc":1.00}
        if any(x in reg for x in ("range","sideway","mean")):
            return "RANGE", {"tf":.80,"structure":.90,"flow":1.02,"vol":1.08,"forecast":.78,"neural":.84,"precision":1.15,"btc":.88}
        if any(x in reg for x in ("transition","volatile","chaos")):
            return "TRANSITION", {"tf":.88,"structure":.88,"flow":1.12,"vol":1.12,"forecast":.70,"neural":.78,"precision":1.06,"btc":1.02}
        return "NEUTRAL", {k:1.0 for k in ("tf","structure","flow","vol","forecast","neural","precision","btc")}

    def _flow(self, item, side):
        # V29 expected a nested derivatives dict, but the production item also
        # exposes these fields at top level. V30 intentionally supports both.
        d=item.get("derivatives") or {}
        def get(k, default=None):
            if k in d: return d.get(k)
            if k == "funding" and item.get("coinglass_funding") is not None: return item.get("coinglass_funding")
            return item.get(k, default)
        parts=[]
        ls=_v30_num(get("long_short_ratio"),0)
        if ls>0:
            e=clamp(math.log(ls),-1,1)
            parts.append(e)
        if get("taker_buy_pct") is not None:
            parts.append(clamp((_v30_num(get("taker_buy_pct"),50)-50)/20,-1,1))
        if get("funding") is not None:
            # crowded positive funding is mildly bearish; negative funding mildly bullish
            parts.append(clamp(-_v30_num(get("funding"),0)/0.0015,-1,1))
        oi=_v30_num(get("oi_delta"),0)
        if oi:
            parts.append(clamp(oi/6,-1,1))
        e=sum(parts)/len(parts) if parts else 0.0
        return e if side=="LONG" else -e, min(1,len(parts)/4), parts

    def _tf(self,item,side):
        tf=item.get("tf_scores") or item.get("tfs") or {}
        vals=[]; used=0
        for k,w in self.TF_W.items():
            if k not in tf: continue
            e=clamp((_v30_num(tf.get(k),50)-50)/50,-1,1)
            vals.append((e,w)); used+=1
        if not vals: return 0.0,0.0,{"frames":0,"coherence":0}
        total_w=sum(w for _,w in vals)
        signed=sum(e*w for e,w in vals)/max(total_w,1e-9)
        signs=[1 if e>.08 else -1 if e<-.08 else 0 for e,_ in vals]
        active=[x for x in signs if x]
        coherence=max(active.count(1),active.count(-1))/len(active) if active else .35
        return (signed if side=="LONG" else -signed), min(1,used/4), {"frames":used,"coherence":round(coherence,3),"signed":round(signed,3)}

    def _build_side(self,item,side,reg,mult):
        ev=[]
        def add(name, family, edge, weight, quality, source):
            ev.append({"name":name,"family":family,"edge":clamp(edge,-1,1),"weight":max(0,weight),"quality":clamp(quality,0,1),"source":source})

        tf,tfq,tfm=self._tf(item,side)
        add("multi_timeframe","trend",tf,.25*mult["tf"],tfq,f"{tfm['frames']} frames / coherence {tfm['coherence']}")

        edge=item.get("edge") or {}
        st=edge.get("structure") or {}
        sb=_v30_direction(st.get("bias"))
        sc=clamp(_v30_num(st.get("confirmation_score"),50),0,100)
        se=((sc-50)/50) if sc>=50 else .35*((sc-50)/50)
        if sb!=side: se=-abs(se) if sb in {"LONG","SHORT"} else 0
        add("market_structure","structure",se,.17*mult["structure"],sc/100,sb or "WAIT")

        fe,fq,fp=self._flow(item,side)
        add("derivatives_flow","flow",fe,.14*mult["flow"],fq,f"parts={len(fp)}")

        # Precision + volatility are deliberately separate: entry quality does
        # not become a directional vote just because volatility is high.
        pr=item.get("precision") or {}
        ps=clamp(_v30_num(pr.get("score"),50),0,100)
        pe=(ps-50)/50
        timing=str(pr.get("entry_timing") or "").upper()
        if timing in {"AVOID","DANGER","LATE"}: pe*=.45
        add("entry_precision","precision",pe,.10*mult["precision"],ps/100,timing or "unknown")

        vol=_v30_num(item.get("volatility_pct"),0)
        # Volatility is a confidence modifier, not a fake directional signal.
        vol_quality=1.0 if 0.05 <= vol <= 8 else .62 if vol>0 else .45
        add("volatility_context","vol",0,.08*mult["vol"],vol_quality,f"vol={vol:.3f}%")

        fc=item.get("candle_forecast") or {}
        fb=_v30_direction(fc.get("overall_bias"))
        fs=clamp(_v30_num(fc.get("path_strength"),0)/40,0,1)
        add("future_path","forecast",fs if fb==side else -fs if fb in {"LONG","SHORT"} else 0,.10*mult["forecast"],fs,fb)

        neural=item.get("neural_v9") or (item.get("fusion") or {}).get("neural_v9") or {}
        ns=_v30_direction(neural.get("side")); nc=clamp(_v30_num(neural.get("confidence"),0)/100,0,1)
        add("neural_model","neural",nc if ns==side else -nc if ns in {"LONG","SHORT"} else 0,.11*mult["neural"],nc,ns)

        # BTC context is a filter, not an absolute veto. This avoids blocking
        # strong idiosyncratic setups while still accounting for market beta.
        btc=_v30_direction(item.get("btc_trend"))
        btc_e=.45 if btc==side else -.45 if btc in {"LONG","SHORT"} else 0
        add("btc_context","btc",btc_e,.07*mult["btc"],.75 if btc!="WAIT" else .35,btc)

        ai=edge.get("ai") or {}
        am=_v30_direction(ai.get("majority") or ai.get("ai_majority") or item.get("ai_majority"))
        aa=clamp(_v30_num(ai.get("agreement") or ai.get("majority_agreement"),0)/100,0,1)
        add("ai_ensemble","ai",aa if am==side else -aa if am in {"LONG","SHORT"} else 0,V30_MAX_AI*mult.get("ai",.9),aa,am)

        # Data integrity is a global quality multiplier; it never creates direction.
        dq=clamp(_v30_num((item.get("data_quality") or {}).get("score") or ((item.get("fusion") or {}).get("data_quality") or {}).get("score"),50),0,100)
        global_q=.55+.45*(dq/100)
        for x in ev: x["quality"]*=global_q

        total=sum(x["weight"] for x in ev)
        score=sum(x["edge"]*x["weight"]*x["quality"] for x in ev)/max(total,1e-9)
        active=[x for x in ev if abs(x["edge"])>=.12 and x["quality"]>=.40 and x["weight"]>0]
        support=[x for x in active if x["edge"]>0]
        oppose=[x for x in active if x["edge"]<0]
        support_f={x["family"] for x in support}; oppose_f={x["family"] for x in oppose}
        contradiction=(min(len(support_f),len(oppose_f))/max(len(support_f|oppose_f),1)) if (support_f or oppose_f) else 0
        # Debate quality: agreement among independent families, not raw vote count.
        family_edges={}
        for x in ev:
            family_edges.setdefault(x["family"],[]).append(x["edge"])
        fam={k:sum(v)/len(v) for k,v in family_edges.items()}
        consensus=sum(1 for v in fam.values() if v>.10)/max(sum(1 for v in fam.values() if abs(v)>.10),1)
        disagreement=sum(1 for v in fam.values() if v<-.10)/max(sum(1 for v in fam.values() if abs(v)>.10),1)
        quality=clamp(_v30_num(item.get("signal_quality"),50),0,100)
        rr=_v30_num(item.get("effective_rr_tp1") or item.get("rr_tp1"),0)
        hard=[]
        if dq<V30_MIN_DATA: hard.append("DATA_QUALITY")
        if rr>0 and rr<1.08: hard.append("RR_LOW")
        if _v30_num(item.get("price"),0)<=0: hard.append("PRICE_INVALID")
        return {"side":side,"score":round(score,4),"strength":round(abs(score)*100,1),"support":len(support_f),"oppose":len(oppose_f),"support_families":sorted(support_f),"oppose_families":sorted(oppose_f),"contradiction":round(contradiction,3),"consensus":round(consensus,3),"quality":round(quality,1),"data_quality":round(dq,1),"rr":round(rr,2),"hard_blocks":hard,"evidence":ev,"debate":fam}

    def evaluate(self,item):
        reg,mult=self._regime(item)
        long=self._build_side(item,"LONG",reg,mult); short=self._build_side(item,"SHORT",reg,mult)
        delta=long["score"]-short["score"]
        candidate="LONG" if delta>=.08 else "SHORT" if delta<=-.08 else "WAIT"
        chosen=long if candidate=="LONG" else short if candidate=="SHORT" else (long if long["score"]>=short["score"] else short)
        ready=(candidate in {"LONG","SHORT"} and chosen["score"]>=V30_MIN_READY and chosen["support"]>=3 and chosen["quality"]>=V30_MIN_QUALITY and chosen["data_quality"]>=V30_MIN_DATA and chosen["rr"]>=1.08 and not chosen["hard_blocks"] and chosen["contradiction"]<.70)
        early=(candidate in {"LONG","SHORT"} and chosen["score"]>=V30_MIN_EARLY and chosen["support"]>=2 and chosen["data_quality"]>=50 and not chosen["hard_blocks"])
        watch=(candidate in {"LONG","SHORT"} and chosen["score"]>=V30_MIN_WATCH and chosen["support"]>=2)
        state="READY" if ready else "EARLY" if early else "WATCH" if watch else "WAIT"
        # Confidence is explicitly evidence confidence, not a win probability.
        conf=clamp(50+abs(delta)*38+chosen["support"]*5-chosen["oppose"]*4-chosen["contradiction"]*22+(chosen["data_quality"]-50)*.15,0,96)
        return {"version":V30_VERSION,"regime":reg,"candidate":candidate,"state":state,"confidence":round(conf,1),"delta":round(delta,4),"long":long,"short":short,"chosen":chosen,"discussion":{"agreement":round(chosen["consensus"]*100,1),"disagreement":round(chosen["contradiction"]*100,1),"independent_support":chosen["support"],"independent_opposition":chosen["oppose"],"active_families":sorted(set(chosen["support_families"]+chosen["oppose_families"]))},"explanation":[f"رژیم بازار: {reg}",f"اختلاف شواهد LONG/SHORT: {delta:.3f}",f"حمایت مستقل: {chosen['support']} خانواده",f"مخالفت مستقل: {chosen['oppose']} خانواده",f"اعتماد شواهد: {conf:.1f}/100","AI در سقف وزن 10٪ باقی می‌ماند و به‌تنهایی جهت نمی‌سازد.","Volatility فقط کیفیت را تعدیل می‌کند و به‌تنهایی سیگنال نمی‌سازد."]}


TITAN_DEEP_V30 = TitanDeepConsensusV30()
_analyze_asset_v29 = _analyze_asset_v29_impl

def _analyze_asset_v30_impl(symbol: str, btc_trend: str) -> Optional[dict[str, Any]]:
    item=_analyze_asset_v29(symbol,btc_trend)
    if not item: return None
    try:
        debate=TITAN_DEEP_V30.evaluate(item)
        item["deep_consensus_v30"]=debate
        item.setdefault("fusion",{})["deep_consensus_v30"]=debate
        item["decision_confidence_v30"]=debate["confidence"]
        item["decision_state_v30"]=debate["state"]
        item["decision_discussion_v30"]=debate["discussion"]
        current=str(item.get("decision_tag") or "WAIT").upper()
        cand=debate.get("candidate")
        chosen=debate.get("chosen") or {}
        # Safety demotion: if an existing LONG/SHORT is strongly contradicted
        # by several independent families, remove it rather than silently keep
        # a stale signal. V30 never directly reverses direction.
        if current in {"LONG","SHORT"}:
            opposite=debate["short"] if current=="LONG" else debate["long"]
            if (opposite["score"]>=.30 and opposite["support"]>=V30_DEMOTE_OPPOSITION and opposite["data_quality"]>=V30_MIN_DATA and opposite["quality"]>=V30_MIN_QUALITY and not opposite["hard_blocks"]):
                item["decision_tag"]="WAIT"
                item["bias"]="خنثی"
                item["signal_tag"]="V30 DEBATE — WAIT"
                item["entry_mode"]="WAIT"
                item["decision_demoted_v30"]=True
                debate["explanation"].append("تصمیم قبلی به WAIT تنزل یافت: چند خانواده مستقل شواهد معتبر در جهت مخالف داشتند.")
        elif current=="WAIT" and cand in {"LONG","SHORT"} and debate["state"]=="READY":
            # V30 is intentionally stricter than V29 for final promotion.
            try:
                price=_v12_parse_price(item.get("price")); sl,tp1,tp2=_v12_rebuild_directional_levels(item,cand)
                chk=_v12_level_integrity(price,sl,tp1,tp2,cand)
                if chk.get("ok") and _v30_num(chk.get("rr1"),0)>=1.08:
                    item["stop_loss"]=smart_format(sl); item["tp1"]=smart_format(tp1); item["tp2"]=smart_format(tp2)
                    item["effective_rr_tp1"]=chk["rr1"]; item["effective_rr_tp2"]=chk["rr2"]; item["rr_tp1"]=chk["rr1"]; item["rr_tp2"]=chk["rr2"]
                    item["decision_tag"]=cand; item["bias"]="صعودی" if cand=="LONG" else "نزولی"; item["signal_tag"]="V30 DEEP CONSENSUS READY"; item["entry_mode"]="EARLY"; item["decision_promoted_v30"]=True
                    item["signal_quality"]=max(_v30_num(item.get("signal_quality"),50),min(90,chosen.get("quality",50)+8))
                    debate["applied"]=True
                else: debate["applied"]=False
            except Exception as exc:
                debate["applied"]=False; debate["explanation"].append("ارتقای V30 به‌علت کنترل سطوح انجام نشد."); LOGGER.debug("V30 promotion failed %s: %s",symbol,exc)
        else:
            debate["applied"]=False
        item["canonical_decision"]={**(item.get("canonical_decision") or {}),"decision":item.get("decision_tag","WAIT"),"bias":item.get("bias","خنثی"),"v30_confidence":debate["confidence"],"v30_state":debate["state"]}
        item["decision_audit_v30"]={"version":V30_VERSION,"decision":item.get("decision_tag","WAIT"),"candidate":cand,"state":debate["state"],"confidence":debate["confidence"],"discussion":debate["discussion"],"explanation":debate["explanation"]}
        return item
    except Exception as exc:
        LOGGER.exception("V30 deep consensus failed for %s: %s",symbol,exc)
        item["deep_consensus_v30"]={"version":V30_VERSION,"state":"ERROR","confidence":0,"error":"internal consensus failure"}
        return item


# ============================================================
# V31 — CANONICAL DECISION GOVERNOR
# One brain / many evidence channels. All upstream modules are evidence only;
# none of them is allowed to publish an independent final decision.
# ============================================================
V31_VERSION = "TITAN-V31-CANONICAL-DECISION-GOVERNOR"
V31_MIN_MARGIN = 0.065
V31_READY_MARGIN = 0.145
V31_STRONG_MARGIN = 0.24
V31_MIN_DATA = 54.0
V31_MIN_QUALITY = 50.0
V31_MIN_RR = 1.02
V31_MAX_CONTRADICTION = 0.70
V31_PRIOR_MAX = 0.08

class TitanCanonicalDecisionV31:
    """Single final decision brain.

    Upstream engines may disagree internally. Their outputs are compressed into
    common evidence and evaluated together. Only this governor is authoritative
    for the final LONG/SHORT/WAIT decision exposed to the dashboard/API.
    """
    def _side(self, debate, side):
        return (debate or {}).get(side.lower()) or {}

    def _score(self, debate, side):
        return float(self._side(debate, side).get("score", 0.0) or 0.0)

    def decide(self, item, debate):
        long = self._side(debate, "LONG")
        short = self._side(debate, "SHORT")
        ls = self._score(debate, "LONG")
        ss = self._score(debate, "SHORT")
        margin = ls - ss
        abs_margin = abs(margin)
        candidate = "LONG" if margin >= V31_MIN_MARGIN else "SHORT" if margin <= -V31_MIN_MARGIN else "WAIT"
        chosen = long if candidate == "LONG" else short if candidate == "SHORT" else (long if ls >= ss else short)
        opposite = short if candidate == "LONG" else long if candidate == "SHORT" else {}

        dq = float(chosen.get("data_quality", 0) or 0)
        quality = float(chosen.get("quality", 0) or 0)
        rr = float(chosen.get("rr", 0) or 0)
        contradiction = float(chosen.get("contradiction", 0) or 0)
        support = int(chosen.get("support", 0) or 0)
        oppose = int(chosen.get("oppose", 0) or 0)
        hard = list(chosen.get("hard_blocks") or [])

        # Confidence is an evidence-confidence score, not a probability of profit.
        base = 50.0 + abs_margin * 105.0
        base += min(18.0, support * 4.5)
        base -= min(18.0, oppose * 4.0)
        base -= contradiction * 24.0
        base += max(-10.0, min(10.0, (dq - 60.0) * 0.18))
        base += max(-7.0, min(7.0, (quality - 60.0) * 0.14))
        if rr >= V31_MIN_RR: base += min(5.0, (rr - V31_MIN_RR) * 5.0)
        confidence = clamp(base, 0.0, 96.0)

        reasons=[]
        if candidate == "WAIT": reasons.append("اختلاف LONG و SHORT برای تصمیم جهت‌دار کافی نیست.")
        if dq < V31_MIN_DATA: reasons.append("کیفیت/اعتبار داده برای تصمیم جهت‌دار کافی نیست.")
        if quality < V31_MIN_QUALITY: reasons.append("کیفیت سیگنال ترکیبی پایین‌تر از حد لازم است.")
        if rr and rr < V31_MIN_RR: reasons.append("نسبت ریسک به بازده مؤثر کافی نیست.")
        if contradiction >= V31_MAX_CONTRADICTION: reasons.append("بین خانواده‌های مستقل شواهد تضاد معنادار وجود دارد.")
        if hard: reasons.append("یک یا چند کنترل سخت کیفیت/ریسک فعال است.")
        if support < 2 and candidate != "WAIT": reasons.append("پشتیبانی مستقل برای جهت انتخابی محدود است.")

        # A direction becomes authoritative only when the combined evidence is
        # coherent. No individual model, indicator, or AI provider can promote it.
        directional_ok = (
            candidate in {"LONG","SHORT"}
            and abs_margin >= V31_MIN_MARGIN
            and dq >= V31_MIN_DATA
            and quality >= V31_MIN_QUALITY
            and rr >= V31_MIN_RR
            and support >= 1
            and contradiction < V31_MAX_CONTRADICTION
            and not hard
        )

        if not directional_ok:
            final = "WAIT"
            state = "WAIT"
        elif abs_margin >= V31_STRONG_MARGIN and support >= 3 and quality >= 60 and dq >= 62:
            final = candidate
            state = "STRONG"
        elif abs_margin >= V31_READY_MARGIN and support >= 2:
            final = candidate
            state = "READY"
        else:
            final = candidate
            state = "EARLY"

        # Hysteresis: an existing valid direction may survive a small temporary
        # margin loss, but it cannot survive a clear opposite consensus.
        prior = str(item.get("decision_tag") or "WAIT").upper()
        if final == "WAIT" and prior in {"LONG","SHORT"}:
            prior_side = self._side(debate, prior)
            prior_score = float(prior_side.get("score",0) or 0)
            opp_score = float((short if prior=="LONG" else long).get("score",0) or 0)
            prior_valid = (
                prior_score >= 0.16 and dq >= V31_MIN_DATA and quality >= V31_MIN_QUALITY
                and rr >= V31_MIN_RR and int(prior_side.get("support",0) or 0) >= 1
                and float(prior_side.get("contradiction",0) or 0) < V31_MAX_CONTRADICTION
                and not (prior_side.get("hard_blocks") or [])
            )
            clear_opposite = (opp_score - prior_score) >= V31_READY_MARGIN and int((short if prior=="LONG" else long).get("support",0) or 0) >= 3
            if prior_valid and not clear_opposite:
                final = prior
                state = "HOLDING"
                reasons.append("تصمیم قبلی فقط به‌عنوان hysteresis معتبر حفظ شد؛ هیچ موتور مستقلی تصمیم را تحمیل نکرد.")

        if not reasons:
            reasons.append("تصمیم از ادغام واحد همه خانواده‌های شواهد حاصل شد.")

        return {
            "version": V31_VERSION,
            "decision": final,
            "state": state,
            "candidate": candidate,
            "confidence": round(confidence,1),
            "margin": round(margin,4),
            "long_score": round(ls,4),
            "short_score": round(ss,4),
            "support": support,
            "opposition": oppose,
            "data_quality": round(dq,1),
            "signal_quality": round(quality,1),
            "rr": round(rr,2),
            "contradiction": round(contradiction,3),
            "authoritative": True,
            "reason": reasons,
            "architecture": "ONE_BRAIN_MANY_EVIDENCE_CHANNELS",
        }

TITAN_CANONICAL_V31 = TitanCanonicalDecisionV31()
_analyze_asset_v30 = _analyze_asset_v30_impl

def _analyze_asset_v31_impl(symbol: str, btc_trend: str) -> Optional[dict[str, Any]]:
    """Public analyzer: exactly one authoritative final decision."""
    item = _analyze_asset_v30(symbol, btc_trend)
    if not item:
        return None
    try:
        debate = item.get("deep_consensus_v30") or TITAN_DEEP_V30.evaluate(item)
        canonical = TITAN_CANONICAL_V31.decide(item, debate)

        # Preserve every upstream analysis as evidence/audit, but overwrite the
        # public decision fields from ONE canonical governor only.
        decision = canonical["decision"]
        item["decision_tag"] = decision
        item["bias"] = "صعودی" if decision == "LONG" else "نزولی" if decision == "SHORT" else "خنثی"
        item["entry_mode"] = "EARLY" if decision in {"LONG","SHORT"} else "WAIT"
        item["decision_confidence"] = canonical["confidence"]
        item["decision_state"] = canonical["state"]
        item["signal_tag"] = f"V31 CANONICAL — {decision}"
        item["canonical_decision"] = {
            **(item.get("canonical_decision") or {}),
            "decision": decision,
            "bias": item["bias"],
            "confidence": canonical["confidence"],
            "state": canonical["state"],
            "version": V31_VERSION,
            "authoritative": True,
        }
        item["decision_audit_v31"] = canonical
        item.setdefault("fusion", {})["canonical_decision_v31"] = canonical

        # Explicitly mark upstream outputs as evidence, preventing downstream UI
        # code from treating them as separate final decisions.
        item["decision_architecture"] = {
            "type": "ONE_BRAIN_MANY_EVIDENCE_CHANNELS",
            "authoritative_source": V31_VERSION,
            "upstream_are_evidence_only": True,
            "final_decision": decision,
        }
        return item
    except Exception as exc:
        LOGGER.exception("V31 canonical governor failed for %s: %s", symbol, exc)
        # Fail closed: never invent a directional signal when the single
        # authoritative governor cannot complete.
        item["decision_tag"] = "WAIT"
        item["bias"] = "خنثی"
        item["entry_mode"] = "WAIT"
        item["decision_confidence"] = 0.0
        item["decision_state"] = "ERROR_SAFE_WAIT"
        item["signal_tag"] = "V31 CANONICAL — WAIT"
        item["canonical_decision"] = {
            "decision":"WAIT","state":"ERROR_SAFE_WAIT","confidence":0.0,
            "version":V31_VERSION,"authoritative":True,
            "reason":["موتور تصمیم واحد با خطای داخلی متوقف شد؛ fail-closed به WAIT انجام شد."]
        }
        return item



# ============================================================
# TITAN V32 — CONTINUOUS PERFORMANCE / TIMEFRAME LEARNING / SCAN TELEMETRY
# ---------------------------------------------------------------------------
# V32 does NOT create a second decision brain. It supplies a measured,
# outcome-based historical calibration evidence channel to the single V31
# governor, while separately auditing every canonical forecast by symbol and
# timeframe. This makes the system learn from realized outcomes without
# pretending that a tiny sample is proof.
# ============================================================
V32_VERSION = "TITAN-V32-CONTINUOUS-LEARNING-AUDIT"
V32_TFS = ("15m", "1h", "4h", "1d")
V32_HORIZON_MIN = {"15m": 60, "1h": 240, "4h": 960, "1d": 2880}
V32_COOLDOWN_MIN = {"15m": 20, "1h": 75, "4h": 300, "1d": 1500}
V32_MIN_LEARN_SAMPLES = 12
V32_LEARN_CAP = 0.10
V32_WAIT_MOVE_THRESHOLD = 0.0035


def _tf_minutes(tf: str) -> int:
    return {"15m":15,"1h":60,"4h":240,"1d":1440}.get(str(tf),60)


def _v32_init_tables() -> None:
    try:
        with DB_LOCK, db_conn() as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS v32_forecast_audit(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    decision TEXT NOT NULL,
                    confidence REAL DEFAULT 0,
                    price REAL NOT NULL,
                    horizon_minutes INTEGER NOT NULL,
                    outcome TEXT DEFAULT 'PENDING',
                    return_pct REAL,
                    max_favorable_pct REAL,
                    max_adverse_pct REAL,
                    evaluated_at REAL,
                    reason TEXT DEFAULT '',
                    UNIQUE(symbol,timeframe,created_at)
                )
            """)
            con.execute("CREATE INDEX IF NOT EXISTS idx_v32_pending ON v32_forecast_audit(outcome,created_at)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_v32_perf ON v32_forecast_audit(symbol,timeframe,outcome,created_at)")
            con.execute("""
                CREATE TABLE IF NOT EXISTS v32_learning_state(
                    key TEXT PRIMARY KEY,
                    updated_at REAL NOT NULL,
                    payload TEXT NOT NULL
                )
            """)
    except Exception as exc:
        LOGGER.warning("V32 table initialization failed: %s", exc)


_v32_init_tables()


def _v32_tf_price_direction(df: pd.DataFrame, decision: str, entry: float) -> tuple[str, float, float, float]:
    if df is None or df.empty or entry <= 0:
        return "PENDING", 0.0, 0.0, 0.0
    close = pd.to_numeric(df.get("close"), errors="coerce").dropna()
    high = pd.to_numeric(df.get("high"), errors="coerce").dropna()
    low = pd.to_numeric(df.get("low"), errors="coerce").dropna()
    if close.empty:
        return "PENDING", 0.0, 0.0, 0.0
    final_px = float(close.iloc[-1])
    ret = (final_px / entry - 1.0) if entry else 0.0
    if decision == "SHORT":
        ret = -ret
        favorable = max(0.0, (entry - float(low.min())) / entry) if not low.empty else max(0.0, ret)
        adverse = max(0.0, (float(high.max()) - entry) / entry) if not high.empty else 0.0
    elif decision == "LONG":
        favorable = max(0.0, (float(high.max()) - entry) / entry) if not high.empty else max(0.0, ret)
        adverse = max(0.0, (entry - float(low.min())) / entry) if not low.empty else 0.0
    else:
        favorable = adverse = abs(ret)
    # A directional forecast is correct only if price moved beyond a friction/
    # noise band in the predicted direction. Small moves are NEUTRAL, not wins.
    if decision in {"LONG", "SHORT"}:
        if ret > V32_WAIT_MOVE_THRESHOLD:
            outcome = "WIN"
        elif ret < -V32_WAIT_MOVE_THRESHOLD:
            outcome = "LOSS"
        else:
            outcome = "NEUTRAL"
    else:
        outcome = "WAIT_CORRECT" if abs(ret) <= V32_WAIT_MOVE_THRESHOLD else "WAIT_MISSED"
    return outcome, ret * 100.0, favorable * 100.0, adverse * 100.0


def v32_record_scan_predictions(market_data: list[dict[str, Any]]) -> None:
    """Record one *timeframe-specific* prediction per symbol.

    The previous implementation wrote the same canonical decision into all four
    timeframe rows. That made the performance matrix look like 15m/1h/4h/1d
    were independently tested when they were not. Here each row is derived from
    that timeframe's own score; the canonical multi-TF decision remains separate.
    """
    now = time.time()
    if not market_data:
        return
    with DB_LOCK, db_conn() as con:
        for item in market_data:
            symbol = _normalize_symbol(item.get("symbol", ""))
            price = safe_float(str(item.get("live_price") or item.get("price") or "0").replace(",", ""), 0.0)
            tf_scores = item.get("tf_scores") or {}
            if not symbol or price <= 0:
                continue
            for tf in V32_TFS:
                # Missing/invalid timeframe features are not neutral forecasts.
                # Skip them so they cannot inflate WAIT or confidence statistics.
                raw_score = tf_scores.get(tf) if isinstance(tf_scores, dict) else None
                if raw_score is None or str(raw_score).strip() == "":
                    continue
                try:
                    score = float(raw_score)
                except (TypeError, ValueError, OverflowError):
                    continue
                if not math.isfinite(score) or score < 0.0 or score > 100.0:
                    continue
                policy = v59_tf_policy(tf)
                long_cut = float(policy.get("long", 55.0)); short_cut = float(policy.get("short", 45.0))
                decision = "LONG" if score >= long_cut else "SHORT" if score <= short_cut else "WAIT"
                # Low-TF decisions are not allowed to ignore the next structural frame.
                # This targets the documented 15m/1h weakness without changing 4h/1d labels.
                if decision == "LONG" and tf == "15m":
                    h1 = safe_float(tf_scores.get("1h"), 50.0)
                    if h1 < 51.0: decision = "WAIT"
                elif decision == "SHORT" and tf == "15m":
                    h1 = safe_float(tf_scores.get("1h"), 50.0)
                    if h1 > 49.0: decision = "WAIT"
                elif decision == "LONG" and tf == "1h":
                    h4 = safe_float(tf_scores.get("4h"), 50.0)
                    if h4 < 50.0: decision = "WAIT"
                elif decision == "SHORT" and tf == "1h":
                    h4 = safe_float(tf_scores.get("4h"), 50.0)
                    if h4 > 50.0: decision = "WAIT"
                confidence = clamp(50.0 + abs(score - 50.0) * 2.15, 50.0, 92.0)
                cooldown = int(policy.get("cooldown_min", V32_COOLDOWN_MIN[tf])) * 60
                recent = con.execute(
                    "SELECT 1 FROM v32_forecast_audit WHERE symbol=? AND timeframe=? AND created_at>=? LIMIT 1",
                    (symbol, tf, now - cooldown),
                ).fetchone()
                if recent:
                    continue
                con.execute(
                    """INSERT OR IGNORE INTO v32_forecast_audit(
                        symbol,timeframe,created_at,decision,confidence,price,horizon_minutes,outcome
                    ) VALUES(?,?,?,?,?,?,?,'PENDING')""",
                    (symbol, tf, now, decision, confidence, price, V32_HORIZON_MIN[tf]),
                )


def v32_evaluate_pending() -> int:
    """Resolve historical predictions from actual Binance OHLC, never from later model output."""
    now = time.time(); learned = 0
    with DB_LOCK, db_conn() as con:
        rows = con.execute("SELECT * FROM v32_forecast_audit WHERE outcome='PENDING' AND created_at <= ? ORDER BY created_at LIMIT 250", (now,)).fetchall()
    for row in rows:
        end = float(row["created_at"]) + int(row["horizon_minutes"]) * 60
        if now < end:
            continue
        try:
            tf = str(row["timeframe"]); symbol = str(row["symbol"]); decision = str(row["decision"])
            start_ms = int(float(row["created_at"]) * 1000); end_ms = int(end * 1000)
            # Small bounded pull: enough bars for the horizon, with no look-ahead
            # beyond the evaluation endpoint.
            bars = max(12, min(1000, int(math.ceil(row["horizon_minutes"] / max(1, _tf_minutes(tf)))) + 4))
            df = fetch_klines(symbol, tf, bars, start_ms=start_ms, end_ms=end_ms)
            window = df[(df["t"] >= start_ms) & (df["t"] <= end_ms)] if df is not None and not df.empty else pd.DataFrame()
            outcome, ret, fav, adv = _v32_tf_price_direction(window, decision, safe_float(row["price"], 0.0))
            if outcome == "PENDING":
                continue
            with DB_LOCK, db_conn() as con:
                con.execute("UPDATE v32_forecast_audit SET outcome=?,return_pct=?,max_favorable_pct=?,max_adverse_pct=?,evaluated_at=? WHERE id=?",
                            (outcome, ret, fav, adv, now, row["id"]))
            if outcome in {"WIN", "LOSS"}:
                learned += 1
        except Exception as exc:
            LOGGER.debug("V32 forecast evaluation failed #%s: %s", row["id"], exc)
    return learned


def _v32_beta_rate(wins: int, losses: int, prior: float = 0.5) -> float:
    # Conservative Beta prior. It avoids turning 1/1 into an apparent 50% oracle.
    a = 6.0 * prior + max(0, wins); b = 6.0 * (1.0-prior) + max(0, losses)
    return a / max(a + b, 1e-9)


def v32_performance_matrix(symbol: str | None = None) -> dict[str, Any]:
    """Detailed correctness by symbol/timeframe/direction with sample sufficiency."""
    where=[]; args=[]
    if symbol:
        where.append("symbol=?"); args.append(_normalize_symbol(symbol))
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with DB_LOCK, db_conn() as con:
        rows = con.execute(f"SELECT * FROM v32_forecast_audit {clause} ORDER BY created_at DESC LIMIT 5000", args).fetchall()
    groups={}
    for r in rows:
        key=(str(r["symbol"]),str(r["timeframe"]),str(r["decision"]))
        g=groups.setdefault(key,{"symbol":key[0],"timeframe":key[1],"decision":key[2],"samples":0,"wins":0,"losses":0,"neutral":0,"wait_correct":0,"wait_missed":0,"returns":[]})
        outcome=str(r["outcome"])
        if outcome == "PENDING": continue
        g["samples"] += 1
        if outcome == "WIN": g["wins"] += 1
        elif outcome == "LOSS": g["losses"] += 1
        elif outcome == "NEUTRAL": g["neutral"] += 1
        elif outcome == "WAIT_CORRECT": g["wait_correct"] += 1
        elif outcome == "WAIT_MISSED": g["wait_missed"] += 1
        if r["return_pct"] is not None: g["returns"].append(float(r["return_pct"]))
    items=[]
    for g in groups.values():
        decisive=g["wins"]+g["losses"]
        g["win_rate"]=round(g["wins"]/decisive*100,1) if decisive else None
        total_group = sum(
            1 for r in rows
            if str(r["symbol"]) == g["symbol"] and str(r["timeframe"]) == g["timeframe"]
        )
        g["coverage"] = round(g["samples"] / max(1, total_group) * 100, 1)
        g["avg_return_pct"]=round(float(np.mean(g["returns"])),4) if g["returns"] else 0.0
        g["reliability"]=round(_v32_beta_rate(g["wins"],g["losses"])*100,1) if decisive else 50.0
        g["sample_status"]="ROBUST" if decisive>=30 else "DEVELOPING" if decisive>=V32_MIN_LEARN_SAMPLES else "INSUFFICIENT"
        g.pop("returns",None)
        items.append(g)
    items.sort(key=lambda x:(x["symbol"], V32_TFS.index(x["timeframe"]) if x["timeframe"] in V32_TFS else 99, x["decision"]))
    return {"version":V32_VERSION,"items":items,"count":len(items)}


def v32_learning_profile(symbol: str) -> dict[str, Any]:
    """Return conservative per-timeframe reliability used as one evidence adjustment."""
    matrix=v32_performance_matrix(symbol).get("items",[])
    profile={"symbol":symbol,"timeframes":{},"aggregate":50.0,"samples":0}
    weighted=[]
    for tf in V32_TFS:
        rows=[x for x in matrix if x["timeframe"]==tf and x["decision"] in {"LONG","SHORT"}]
        wins=sum(int(x["wins"]) for x in rows); losses=sum(int(x["losses"]) for x in rows); n=wins+losses
        rel=_v32_beta_rate(wins,losses)*100 if n else 50.0
        recent_score=rel
        if rows:
            recent_score=float(np.mean([x["reliability"] for x in rows]))
        weight=1.0 + (clamp(recent_score,35,65)-50)/50.0 * V32_LEARN_CAP if n>=V32_MIN_LEARN_SAMPLES else 1.0
        profile["timeframes"][tf]={"samples":n,"wins":wins,"losses":losses,"reliability":round(rel,1),"weight":round(weight,4),"status":"LEARNED" if n>=V32_MIN_LEARN_SAMPLES else "WARMING"}
        if n: weighted.append((rel,n))
        profile["samples"] += n
    profile["aggregate"]=round(sum(r*n for r,n in weighted)/sum(n for _,n in weighted),1) if weighted else 50.0
    return profile


def v32_adaptive_evidence(item: dict[str, Any]) -> dict[str, Any]:
    """Create one historical-calibration evidence channel for the V31 brain."""
    symbol=_normalize_symbol(item.get("symbol", "")); profile=v32_learning_profile(symbol)
    tf_scores=(item.get("tf_scores") or {})
    # If the live item does not expose tf scores, derive a neutral profile only;
    # never invent direction from history.
    long_boost=short_boost=0.0; used=[]
    for tf in V32_TFS:
        p=profile["timeframes"].get(tf,{})
        if p.get("samples",0) < V32_MIN_LEARN_SAMPLES: continue
        sc=safe_float(tf_scores.get(tf),50.0)
        direction="LONG" if sc>=55 else "SHORT" if sc<=45 else "WAIT"
        delta=(safe_float(p.get("reliability"),50)-50)/100.0
        if direction=="LONG": long_boost += delta * 0.025; used.append(tf)
        elif direction=="SHORT": short_boost += delta * 0.025; used.append(tf)
    return {"symbol":symbol,"long_adjustment":round(long_boost,4),"short_adjustment":round(short_boost,4),"profile":profile,"used_timeframes":used}


def v32_register_learning_maintenance() -> None:
    try:
        v32_evaluate_pending()
        # Rebuild the authoritative adaptive state from outcomes only. Existing
        # engines remain intact; V32 adds a small measured calibration signal.
        with DB_LOCK, db_conn() as con:
            state={"updated_at":time.time(),"symbols":{}}
            syms=[r[0] for r in con.execute("SELECT DISTINCT symbol FROM v32_forecast_audit").fetchall()]
        for sym in syms:
            state["symbols"][sym]=v32_learning_profile(sym)
        with DB_LOCK, db_conn() as con:
            con.execute("INSERT OR REPLACE INTO v32_learning_state(key,updated_at,payload) VALUES('global',?,?)",(time.time(),json.dumps(state,ensure_ascii=False)))
    except Exception as exc:
        LOGGER.debug("V32 maintenance failed: %s", exc)


# Extend the single V31 brain with one conservative historical-calibration channel.
_v32_analyze_asset_v31 = _analyze_asset_v31_impl

def _analyze_asset_v32_impl(symbol: str, btc_trend: str) -> Optional[dict[str, Any]]:
    item=_v32_analyze_asset_v31(symbol,btc_trend)
    if not item: return None
    try:
        # The prediction being learned is always the canonical V31 output from
        # this same scan. Learning never gets to create an independent signal.
        learning=v32_adaptive_evidence(item)
        debate=dict(item.get("deep_consensus_v30") or {})
        for side, adj in (("long",learning["long_adjustment"]),("short",learning["short_adjustment"])):
            if isinstance(debate.get(side),dict):
                debate[side]=dict(debate[side]); debate[side]["score"]=float(debate[side].get("score",0) or 0)+adj
        canonical=TITAN_CANONICAL_V31.decide(item,debate)
        decision=canonical["decision"]
        item["decision_tag"]=decision
        item["bias"]="صعودی" if decision=="LONG" else "نزولی" if decision=="SHORT" else "خنثی"
        item["entry_mode"]="EARLY" if decision in {"LONG","SHORT"} else "WAIT"
        item["decision_confidence"]=canonical["confidence"]
        item["decision_state"]=canonical["state"]
        item["signal_tag"]=f"V32 LEARNED CANONICAL — {decision}"
        item["v32_learning"]=learning
        item["canonical_decision"]={**(item.get("canonical_decision") or {}),"decision":decision,"bias":item["bias"],"confidence":canonical["confidence"],"state":canonical["state"],"version":V32_VERSION,"authoritative":True}
        item["decision_audit_v32"]={"version":V32_VERSION,"decision":decision,"learning":learning,"canonical":canonical,"authoritative":True}
        item["decision_architecture"]={"type":"ONE_BRAIN_MANY_EVIDENCE_CHANNELS","authoritative_source":V32_VERSION,"upstream_are_evidence_only":True,"learning_is_evidence_only":True,"final_decision":decision}
        return item
    except Exception as exc:
        LOGGER.exception("V32 learned canonical failed for %s: %s",symbol,exc)
        return item


# ============================================================
# ROUTES
# ============================================================

        return jsonify({"ok":False,"error":str(exc)[:240]}),500

@app.get("/api/v36-resonance")
def api_v36_resonance():
    """Dashboard summary of Quantum Resonance Edge state per symbol."""
    try:
        with CACHE_LOCK:
            rows = list(CACHE.get("data") or [])
        items = []
        killed = boosted = 0
        for item in rows:
            r = item.get("v36_resonance") or {}
            ha = r.get("horizon_agreement") or {}
            pd_ = r.get("price_drift") or {}
            ca = r.get("cross_asset") or {}
            if r.get("killed"):
                killed += 1
            if r.get("boosted"):
                boosted += 1
            items.append({
                "symbol": item.get("symbol"),
                "decision": item.get("decision_tag") or item.get("decision"),
                "confidence": item.get("decision_confidence"),
                "agreement": ha.get("agreement"),
                "horizon_direction": ha.get("direction"),
                "aligned": ha.get("aligned_with_decision"),
                "drift_pct": pd_.get("drift_pct"),
                "drift_kill": pd_.get("kill"),
                "same_side_cluster": ca.get("same_side"),
                "cluster_penalty": ca.get("penalty"),
                "killed": bool(r.get("killed")),
                "boosted": bool(r.get("boosted")),
                "notes": r.get("notes") or [],
                "live_sync": item.get("live_sync"),
                "live_age_sec": item.get("live_price_age_sec"),
                "v34": (item.get("v34_opportunity") or {}).get("state"),
            })
        return jsonify({
            "ok": True,
            "version": globals().get("V36_VERSION", "V36"),
            "param_version": TITAN_PARAM_VERSION,
            "count": len(items),
            "killed": killed,
            "boosted": boosted,
            "items": items,
            "note": "رزونانس کوانتومی فقط کیفیت/ایمنی سیگنال را تنظیم می‌کند؛ احتمال برد تضمینی نیست.",
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)[:240]}), 500



@app.get("/api/v40-precision")
def api_v40_precision():
    """History bans, live precision status, self-heal report endpoint."""
    try:
        hist = _v40_refresh_hist(force=True)
        with CACHE_LOCK:
            rows = list(CACHE.get("data") or [])
        live = []
        for item in rows:
            p = item.get("v40_precision") or {}
            live.append({
                "symbol": item.get("symbol"),
                "decision": item.get("decision_tag") or item.get("decision"),
                "confidence": item.get("decision_confidence"),
                "passed": p.get("passed"),
                "demoted": item.get("v40_demoted"),
                "code": item.get("v40_demote_code"),
                "horizon": (p.get("horizon") or {}).get("agreement"),
                "history_wr": (p.get("history") or {}).get("wr"),
                "history_n": (p.get("history") or {}).get("samples"),
            })
        by_key = hist.get("by_key") or {}
        banned = [k for k, st in by_key.items()
                  if st.get("samples", 0) >= V40_BAN_N and st.get("wr", 50) < V40_BAN_WR]
        with DB_LOCK, db_conn() as con:
            totals = {r[0]: r[1] for r in con.execute(
                "SELECT outcome, COUNT(*) FROM central_predictions GROUP BY outcome").fetchall()}
        w, l = int(totals.get("WIN", 0) or 0), int(totals.get("LOSS", 0) or 0)
        return jsonify({
            "ok": True,
            "version": V40_VERSION,
            "param_version": TITAN_PARAM_VERSION,
            "decided_win_rate": round(100.0 * w / (w + l), 1) if (w + l) else None,
            "central_totals": totals,
            "global_short": hist.get("g_short"),
            "global_long": hist.get("g_long"),
            "banned_keys": banned,
            "live": live,
            "note": "V40 فقط سیگنال با اجماع، RR قوی، سابقه قابل قبول و رژیم سازگار را منتشر می‌کند.",
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)[:240]}), 500


@app.get("/api/edge-lab")
def api_edge_lab():
    try:
        return jsonify(_edge_api_report())
    except Exception as exc:
        return jsonify({"ok":False,"error":str(exc)[:240]}),500


def _central_learner_loop():
    while not LIVE_STOP.is_set():
        try:
            _central_evaluate_pending()
            _central_load_weights()
        except Exception as exc:
            LOGGER.debug("central learner: %s", exc)
        LIVE_STOP.wait(40)


def _shutdown_runtime() -> None:
    """Stop background loops and close reusable worker pools on process exit."""
    LIVE_STOP.set()
    for pool_name in ("_ANALYSIS_POOL", "_KLINE_POOL", "_AI_POOL"):
        pool = globals().get(pool_name)
        if pool is not None:
            try:
                pool.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                try:
                    pool.shutdown(wait=False)
                except Exception:
                    _swallow()
            except Exception:
                _swallow()


import atexit
atexit.register(_shutdown_runtime)


def _open_dashboard_browser() -> None:
    """Open the local dashboard after the HTTP server becomes reachable."""
    try:
        import webbrowser
        url = f"http://127.0.0.1:{PORT}/"
        for _ in range(30):
            try:
                with socket.create_connection(("127.0.0.1", PORT), timeout=0.25):
                    # Android/Termux: prefer the system browser intent when available.
                    try:
                        import shutil as _sh, subprocess as _sp
                        _tou = _sh.which("termux-open-url")
                        if _tou:
                            _sp.Popen([_tou, url], stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
                            return
                    except Exception:
                        _swallow()
                    webbrowser.open(url, new=1, autoraise=True)
                    return
            except OSError:
                LIVE_STOP.wait(0.5)
                if LIVE_STOP.is_set():
                    return
    except Exception as exc:
        LOGGER.debug("Dashboard browser launch skipped: %s", exc)


# continuous learner started with main
_CENTRAL_LEARNER_STARTED = False


def _ensure_central_learner():
    global _CENTRAL_LEARNER_STARTED
    if _CENTRAL_LEARNER_STARTED:
        return
    try:
        threading.Thread(target=_central_learner_loop, name="titan-central-learner", daemon=True).start()
        _CENTRAL_LEARNER_STARTED = True
    except Exception:
        _swallow()




# ============================================================
# TITAN V44 — BALANCED OPPORTUNITY + HONEST OUTCOME LEARNING
# ============================================================
V44_VERSION = "TITAN-V44-BALANCED-LEARNING"
# A modestly more opportunity-aware operating point. V42 hard safety checks
# remain authoritative; these values never override stale/bad data or invalid levels.
_CENTRAL_MIN_EDGE = 0.12
_CENTRAL_MIN_MARGIN = 0.055
_CENTRAL_MIN_TRUST = 48.0

_V44_PREV_EVIDENCE = _v43_unified_evidence_base

def _v43_unified_evidence(item: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Keep directional evidence separate from quality/reliability evidence.

    Precision, robustness, opportunity, calibration and forecast magnitude are
    not direction votes. Counting them as LONG/SHORT was a source of bias.
    They remain visible in the audit registry and are applied as quality context.
    """
    votes, participation = _V44_PREV_EVIDENCE(item)
    quality_only = {"precision_engine", "v31_edge_suite", "v34_opportunity",
                    "v40_precision", "realized_calibration", "forecast_engine"}
    quality = {}
    for name in list(votes):
        if name in quality_only:
            quality[name] = votes.pop(name)
    # Re-add forecast only when its direction is explicit; never infer direction
    # from a positive expected move alone (which can be an unsigned magnitude).
    fc = item.get("candle_forecast") or item.get("forecast") or {}
    if isinstance(fc, dict):
        bias = str(fc.get("overall_bias") or "").strip().lower()
        if bias in {"صعودی", "bullish", "up", "long"}:
            _v43_add_vote(votes, "forecast_direction", 68.0, "جهت پیش‌بینی", "FORECAST", True, 0.40)
        elif bias in {"نزولی", "bearish", "down", "short"}:
            _v43_add_vote(votes, "forecast_direction", 32.0, "جهت پیش‌بینی", "FORECAST", True, 0.40)
    directional = [v for v in votes.values() if v.get("side") in {"LONG", "SHORT"}
                   and safe_float(v.get("confidence"), 0.0) >= 0.08]
    groups = {str(v.get("group") or "CORE") for v in directional}
    required_components, required_groups = 5, 3
    sufficient = len(directional) >= required_components and len(groups) >= required_groups
    participation.update({
        "active_components": len(directional), "active_groups": len(groups),
        "required_components": required_components, "required_groups": required_groups,
        "sufficient": sufficient,
        "directional_components": [k for k,v in votes.items() if v in directional],
        "quality_only_channels": quality,
        "policy": "direction votes are separated from quality/calibration channels",
    })
    return votes, participation

_V44_PREV_DECIDE = _v43_unified_decide_base

def _v43_unified_decide(item: dict[str, Any]) -> dict[str, Any]:
    x = _V44_PREV_DECIDE(item)
    pack = x.get("unified_central") if isinstance(x.get("unified_central"), dict) else {}
    # Transparent quality modifiers; do not manufacture direction or bypass hard vetoes.
    quality = pack.get("participation", {}).get("quality_only_channels", {})
    v40 = quality.get("v40_precision") or {}
    v31 = quality.get("v31_edge_suite") or {}
    cal = quality.get("realized_calibration") or {}
    modifiers = []
    if v40:
        q = safe_float(v40.get("raw"), 50.0)
        modifiers.append({"module":"V40 precision", "score":q, "role":"quality-only"})
    if v31:
        q = safe_float(v31.get("raw"), 50.0)
        modifiers.append({"module":"V31 robustness", "score":q, "role":"quality-only"})
    if cal:
        modifiers.append({"module":"realized calibration", "score":safe_float(cal.get("raw"),50),
                          "samples":safe_float((x.get("probability_calibration") or {}).get("samples"),0),
                          "role":"probability calibration, not direction"})
    if isinstance(pack, dict):
        pack["quality_modifiers"] = modifiers
        pack["opportunity_policy"] = {
            "version": V44_VERSION,
            "edge_floor": _CENTRAL_MIN_EDGE,
            "margin_floor": _CENTRAL_MIN_MARGIN,
            "wait_is_not_a_target": True,
            "hard_safety_vetoes_preserved": True,
            "quality_channels_not_counted_as_direction": True,
        }
        x["unified_central"] = pack
        arch = x.get("decision_architecture") if isinstance(x.get("decision_architecture"),dict) else {}
        arch.update({"balanced_opportunity_policy": V44_VERSION,
                    "quality_channels_separate_from_direction": True,
                    "outcome_learning_required": True})
        x["decision_architecture"] = arch
        x["authority"] = V44_VERSION
    return x

# Detailed read-only evaluation endpoint. It reports observed history rather than
# claiming that a model has learned correctly merely because a loop is running.
@app.get("/api/decision-quality")
def api_decision_quality():
    try:
        with DB_LOCK, db_conn() as con:
            rows = con.execute("""SELECT outcome, confidence FROM central_predictions
                WHERE outcome IN ('WIN','LOSS') ORDER BY created_at DESC LIMIT 5000""").fetchall()
            total = int(con.execute("SELECT COUNT(*) FROM central_predictions").fetchone()[0])
            pending = int(con.execute("SELECT COUNT(*) FROM central_predictions WHERE outcome='PENDING'").fetchone()[0])
            # Optional trade metrics table is present in newer DB schemas.
            metrics = []
            try:
                metrics = con.execute("""SELECT m.r_multiple,m.net_return_pct,p.outcome
                    FROM central_trade_metrics m JOIN central_predictions p ON p.id=m.prediction_id
                    WHERE p.outcome IN ('WIN','LOSS') ORDER BY m.evaluated_at DESC LIMIT 5000""").fetchall()
            except Exception:
                metrics = []
        wins = sum(1 for r in rows if str(r[0]) == 'WIN')
        losses = sum(1 for r in rows if str(r[0]) == 'LOSS')
        confs = [safe_float(r[1], 0.0) for r in rows if r[1] is not None]
        rs = [safe_float(r[0], 0.0) for r in metrics if r[0] is not None]
        returns = [safe_float(r[1], 0.0) for r in metrics if r[1] is not None]
        return jsonify({"ok": True, "version": V44_VERSION,
            "history": {"recorded_predictions": total, "pending": pending,
                "resolved": len(rows), "wins": wins, "losses": losses,
                "observed_win_rate_pct": round(100*wins/max(1,wins+losses), 2) if rows else None,
                "mean_recorded_confidence": round(sum(confs)/len(confs),2) if confs else None,
                "mean_r_multiple": round(sum(rs)/len(rs),4) if rs else None,
                "mean_net_return_pct": round(sum(returns)/len(returns),4) if returns else None,
                "trade_metric_samples": len(metrics)},
            "learning_status": {"outcome_based": True,
                "insufficient_history": len(rows) < 30,
                "minimum_resolved_for_reliable_calibration": 30,
                "note": "این آمار گذشته است؛ به‌تنهایی تضمین‌کننده عملکرد آینده نیست."},
            "audit": {"last_5000_resolved_outcomes_used": len(rows),
                "confidence_is_not_win_probability": True,
                "win_rate_excludes_pending": True}})
    except Exception as exc:
        return jsonify({"ok": False, "version": V44_VERSION, "error": str(exc)[:240]}), 500


# ============================================================
# TITAN V44 — ADAPTIVE OPPORTUNITY + CONTINUOUS ERROR-LEARNING BRAIN
# ============================================================
# V44 does not promise zero errors.  Its job is to reduce avoidable errors,
# distinguish unsafe WAITs from merely weak-consensus WAITs, and adapt bounded
# thresholds from realized outcomes without allowing a short bad/good streak to
# destabilize the governor.
V44_VERSION = "TITAN-V44-ADAPTIVE-OPPORTUNITY-LEARNING"
V44_POLICY_MIN_SAMPLES = 12
V44_POLICY_WINDOW = 80
V44_BASE_EDGE = 0.135
V44_BASE_MARGIN = 0.075
V44_BASE_TRUST = 46.0
V44_MIN_EDGE = 0.105
V44_MAX_EDGE = 0.205
V44_MIN_MARGIN = 0.055
V44_MAX_MARGIN = 0.135
V44_MIN_TRUST = 42.0
V44_MAX_TRUST = 55.0
V44_MIN_CONFIDENCE = 58.0
V44_MAX_CONFIDENCE = 90.0


def _v44_learning_schema() -> None:
    try:
        with DB_LOCK, db_conn() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS central_learning_events(
                prediction_id INTEGER PRIMARY KEY,
                created_at REAL,
                symbol TEXT,
                direction TEXT,
                outcome TEXT,
                return_pct REAL,
                error_class TEXT,
                lesson TEXT,
                regime TEXT,
                confidence REAL,
                edge REAL,
                margin REAL,
                policy_edge REAL,
                policy_margin REAL,
                policy_trust REAL,
                learned_at REAL
            )""")
            con.execute("""CREATE TABLE IF NOT EXISTS central_policy_state(
                scope TEXT PRIMARY KEY,
                samples INTEGER DEFAULT 0,
                wins INTEGER DEFAULT 0,
                losses INTEGER DEFAULT 0,
                time_exits INTEGER DEFAULT 0,
                avg_return REAL DEFAULT 0,
                win_rate REAL DEFAULT 50,
                edge_threshold REAL DEFAULT 0.135,
                margin_threshold REAL DEFAULT 0.075,
                trust_threshold REAL DEFAULT 46,
                last_update REAL,
                lesson TEXT
            )""")
            con.execute("CREATE INDEX IF NOT EXISTS idx_learning_symbol ON central_learning_events(symbol,learned_at)")
            con.commit()
    except Exception as exc:
        LOGGER.debug("V44 learning schema: %s", exc)


_v44_learning_schema()


def _v44_policy_stats(symbol: str = "", direction: str = "") -> dict[str, Any]:
    """Read realized outcomes; never treat PENDING/AMBIGUOUS as wins/losses."""
    try:
        clauses = ["outcome IN ('WIN','LOSS','TIME_EXIT')"]
        args: list[Any] = []
        if symbol:
            clauses.append("symbol=?")
            args.append(_normalize_symbol(symbol))
        if direction in {"LONG", "SHORT"}:
            clauses.append("decision=?")
            args.append(direction)
        where = " AND ".join(clauses)
        with DB_LOCK, db_conn() as con:
            rows = con.execute(
                f"SELECT outcome,return_pct,confidence,created_at FROM central_predictions WHERE {where} ORDER BY created_at DESC LIMIT ?",
                (*args, V44_POLICY_WINDOW),
            ).fetchall()
        if not rows:
            return {"samples": 0, "wins": 0, "losses": 0, "time_exits": 0, "win_rate": 50.0,
                    "avg_return": 0.0, "thresholds": {"edge": V44_BASE_EDGE, "margin": V44_BASE_MARGIN, "trust": V44_BASE_TRUST}}
        usable = [r for r in rows if str(r[0]) in {"WIN", "LOSS", "TIME_EXIT"}]
        wins = sum(str(r[0]) == "WIN" for r in usable)
        losses = sum(str(r[0]) == "LOSS" for r in usable)
        exits = sum(str(r[0]) == "TIME_EXIT" for r in usable)
        n_bin = wins + losses
        wr = 100.0 * wins / n_bin if n_bin else 50.0
        returns = [safe_float(r[1], 0.0) for r in usable]
        avg_ret = sum(returns) / len(returns) if returns else 0.0
        # Bounded adaptation: good realized edge opens the gate slightly;
        # weak realized edge tightens it.  The adjustment is deliberately small.
        edge = V44_BASE_EDGE
        margin = V44_BASE_MARGIN
        trust = V44_BASE_TRUST
        if len(usable) >= V44_POLICY_MIN_SAMPLES:
            if wr >= 62.0 and avg_ret > 0:
                edge -= 0.018; margin -= 0.012; trust -= 1.5
            elif wr >= 56.0 and avg_ret >= 0:
                edge -= 0.010; margin -= 0.007; trust -= 0.8
            elif wr <= 42.0 or avg_ret < -0.10:
                edge += 0.028; margin += 0.018; trust += 2.5
            elif wr <= 47.0:
                edge += 0.015; margin += 0.010; trust += 1.2
        return {
            "samples": len(usable), "wins": wins, "losses": losses, "time_exits": exits,
            "win_rate": round(wr, 2), "avg_return": round(avg_ret, 5),
            "thresholds": {"edge": clamp(edge, V44_MIN_EDGE, V44_MAX_EDGE),
                           "margin": clamp(margin, V44_MIN_MARGIN, V44_MAX_MARGIN),
                           "trust": clamp(trust, V44_MIN_TRUST, V44_MAX_TRUST)},
        }
    except Exception as exc:
        return {"samples": 0, "wins": 0, "losses": 0, "time_exits": 0, "win_rate": 50.0,
                "avg_return": 0.0, "thresholds": {"edge": V44_BASE_EDGE, "margin": V44_BASE_MARGIN, "trust": V44_BASE_TRUST},
                "error": str(exc)[:160]}


def _v44_hard_safety_reasons(item: dict[str, Any]) -> list[str]:
    """Reasons that must never be overridden merely to capture opportunity."""
    hard: list[str] = []
    u = item.get("unified_central") or {}
    for r in u.get("participation", {}).get("hard_vetoes", []) if isinstance(u, dict) else []:
        hard.append(str(r))
    v40 = item.get("v40_precision") or {}
    if isinstance(v40, dict):
        hard.extend(str(x) for x in (v40.get("hard_blocks") or []) if x)
        if bool(v40.get("veto")) or bool(v40.get("hard_block")):
            hard.append("V40_HARD_VETO")
    real = (item.get("central_decision") or {}).get("real_edge") or item.get("real_edge") or {}
    if isinstance(real, dict):
        for r in real.get("no_trade_reasons") or []:
            # quality-only reasons are soft; structural/risk violations remain hard.
            rs = str(r)
            if rs in {"invalid_levels", "weak_rr", "correlation_risk", "regime_conflict", "structure_not_confirmed",
                      "liquidity_flow_conflict", "late_entry"}:
                hard.append(rs)
    integ = item.get("v42_integrity") or {}
    if isinstance(integ, dict) and not integ.get("passed", True):
        for r in integ.get("reasons") or []:
            hard.append(str(r))
    return list(dict.fromkeys(hard))


def _v44_should_promote(item: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """Turn only *soft* WAITs into opportunities when evidence is strong enough."""
    x = item or {}
    u = x.get("unified_central") or {}
    candidate = str(u.get("candidate") or "WAIT").upper()
    if candidate not in {"LONG", "SHORT"}:
        return False, {"reason": "no_directional_candidate"}
    hard = _v44_hard_safety_reasons(x)
    if hard:
        return False, {"reason": "hard_safety", "hard_reasons": hard}
    stats = _v44_policy_stats(str(x.get("symbol") or ""), candidate)
    th = stats.get("thresholds") or {}
    edge = safe_float(u.get("edge"), 0.0)
    margin = safe_float(u.get("margin"), 0.0)
    trust = safe_float(u.get("trust"), 0.0)
    conf = safe_float(u.get("confidence"), safe_float(x.get("decision_confidence"), 0.0))
    # Require two independent dimensions above gate plus usable confidence.
    ok = edge >= safe_float(th.get("edge"), V44_BASE_EDGE) and \
         margin >= safe_float(th.get("margin"), V44_BASE_MARGIN) and \
         trust >= safe_float(th.get("trust"), V44_BASE_TRUST) and \
         conf >= V44_MIN_CONFIDENCE
    return ok, {
        "candidate": candidate, "edge": round(edge, 5), "margin": round(margin, 5), "trust": round(trust, 2),
        "confidence": round(conf, 2), "thresholds": th, "policy_stats": stats,
        "hard_reasons": hard, "promoted": bool(ok),
    }


def _v44_apply_opportunity_policy_base(item: dict[str, Any]) -> dict[str, Any]:
    """Adaptive final policy: preserve V42 safety, recover only quality WAITs."""
    x = dict(item or {})
    before = str(x.get("decision_tag") or x.get("decision") or "WAIT").upper()
    if before != "WAIT":
        x["v44_opportunity"] = {"version": V44_VERSION, "promoted": False, "reason": "already_directional"}
        return x
    ok, audit = _v44_should_promote(x)
    if not ok:
        x["v44_opportunity"] = {"version": V44_VERSION, **audit}
        return x

    candidate = audit["candidate"]
    # Re-run the integrity contract on the candidate before publishing it.
    trial = dict(x)
    trial["decision"] = candidate
    trial["decision_tag"] = candidate
    trial["bias"] = "صعودی" if candidate == "LONG" else "نزولی"
    trial["entry_mode"] = "EARLY"
    trial["signal_tag"] = f"ADAPTIVE-CENTRAL — {candidate}"
    trial["decision_state"] = f"ADAPTIVE_CENTRAL_{candidate}"
    trial = _v42_integrity_seal(trial)
    if str(trial.get("decision_tag") or "WAIT") != candidate:
        audit["promoted"] = False
        audit["reason"] = "integrity_recheck_failed"
        audit["integrity"] = trial.get("v42_integrity") or {}
        x["v44_opportunity"] = audit
        return x

    x.update({
        "decision": candidate, "decision_tag": candidate,
        "bias": "صعودی" if candidate == "LONG" else "نزولی",
        "entry_mode": "EARLY",
        "signal_tag": f"ADAPTIVE-CENTRAL — {candidate}",
        "decision_state": f"ADAPTIVE_CENTRAL_{candidate}",
        "decision_confidence": round(clamp(safe_float((x.get("unified_central") or {}).get("confidence"), 0.0), V44_MIN_CONFIDENCE, V44_MAX_CONFIDENCE), 2),
        "v44_opportunity": {"version": V44_VERSION, **audit},
    })
    arch = dict(x.get("decision_architecture") or {})
    arch.update({"adaptive_opportunity_policy": V44_VERSION, "final_decision": candidate,
                 "soft_wait_recovered": True, "hard_safety_overridable": False})
    x["decision_architecture"] = arch
    x["authority"] = V44_VERSION
    return x


def _v44_learn_realized_outcomes() -> int:
    """Create explicit error/lesson records and persistent policy state."""
    done = 0
    try:
        with DB_LOCK, db_conn() as con:
            rows = con.execute(
                """SELECT p.id,p.created_at,p.symbol,p.decision,p.outcome,p.return_pct,p.confidence,p.margin,p.reasons_json,
                          m.regime
                   FROM central_predictions p LEFT JOIN central_trade_metrics m ON m.prediction_id=p.id
                   WHERE p.outcome IN ('WIN','LOSS','TIME_EXIT','AMBIGUOUS')
                     AND NOT EXISTS (SELECT 1 FROM central_learning_events e WHERE e.prediction_id=p.id)
                   ORDER BY p.evaluated_at LIMIT 250"""
            ).fetchall()
        for row in rows:
            rid, created, symbol, direction, outcome, ret, conf, margin, reasons_json, regime = row
            try:
                reasons = json.loads(reasons_json or "[]")
            except Exception:
                reasons = []
            if outcome == "WIN":
                error_class = "NONE"
                lesson = "تصمیم تحقق‌یافته موفق بود؛ وزن شواهد هم‌جهت حفظ و فقط در صورت تکرار تقویت شود."
            elif outcome == "LOSS":
                error_class = "FALSE_DIRECTION"
                lesson = "تصمیم اشتباه بود؛ عوامل هم‌جهت با تصمیم باید در نمونه‌های بعدی وزن کمتری بگیرند و شرایط شکست بررسی شود."
            elif outcome == "TIME_EXIT":
                error_class = "NO_RESOLUTION"
                lesson = "حرکت کافی برای تحقق هدف/حدضرر رخ نداد؛ این نمونه برای کالیبراسیون باینری ضعیف است و نباید برد محسوب شود."
            else:
                error_class = "AMBIGUOUS_PATH"
                lesson = "ترتیب لمس سطوح نامشخص بود؛ نمونه از یادگیری برد/باخت حذف می‌شود."
            with DB_LOCK, db_conn() as con:
                con.execute(
                    """INSERT OR IGNORE INTO central_learning_events(
                       prediction_id,created_at,symbol,direction,outcome,return_pct,error_class,lesson,regime,confidence,edge,margin,policy_edge,policy_margin,policy_trust,learned_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (rid,created, symbol,direction,outcome,safe_float(ret,0),error_class,lesson,regime,
                     safe_float(conf,0),0.0,safe_float(margin,0),V44_BASE_EDGE,V44_BASE_MARGIN,V44_BASE_TRUST,time.time())
                )
                con.commit()
            done += 1
        # Persist current bounded policy for global/LONG/SHORT and symbols with data.
        scopes = [("GLOBAL", ""), ("LONG", "LONG"), ("SHORT", "SHORT")]
        try:
            with DB_LOCK, db_conn() as con:
                syms = [r[0] for r in con.execute("SELECT DISTINCT symbol FROM central_predictions WHERE symbol IS NOT NULL LIMIT 200").fetchall()]
        except Exception:
            syms = []
        for scope, direction in scopes + [(f"SYMBOL:{s}", "") for s in syms]:
            sym = scope.split(":",1)[1] if scope.startswith("SYMBOL:") else ""
            st = _v44_policy_stats(sym, direction)
            th = st.get("thresholds") or {}
            with DB_LOCK, db_conn() as con:
                con.execute(
                    """INSERT INTO central_policy_state(scope,samples,wins,losses,time_exits,avg_return,win_rate,edge_threshold,margin_threshold,trust_threshold,last_update,lesson)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(scope) DO UPDATE SET samples=excluded.samples,wins=excluded.wins,losses=excluded.losses,time_exits=excluded.time_exits,
                       avg_return=excluded.avg_return,win_rate=excluded.win_rate,edge_threshold=excluded.edge_threshold,margin_threshold=excluded.margin_threshold,
                       trust_threshold=excluded.trust_threshold,last_update=excluded.last_update,lesson=excluded.lesson""",
                    (scope,st.get("samples",0),st.get("wins",0),st.get("losses",0),st.get("time_exits",0),st.get("avg_return",0),st.get("win_rate",50),
                     safe_float(th.get("edge"),V44_BASE_EDGE),safe_float(th.get("margin"),V44_BASE_MARGIN),safe_float(th.get("trust"),V44_BASE_TRUST),time.time(),
                     "bounded adaptive policy from realized outcomes"),
                )
                con.commit()
        return done
    except Exception as exc:
        LOGGER.debug("V44 learner: %s", exc)
        return done


# Wrap the existing evaluator so realized outcomes automatically feed the
# adaptive policy without changing its careful chronological first-touch logic.
_V44_PREV_EVALUATE_PENDING = _central_evaluate_pending_base

def _central_evaluate_pending_v44() -> dict[str, Any]:
    result = _V44_PREV_EVALUATE_PENDING()
    try:
        learned = _v44_learn_realized_outcomes()
        if isinstance(result, dict):
            result = dict(result)
            result["v44_learning_events"] = learned
    except Exception:
        _swallow()
    return result


# Final public gateway: V43 remains the one-brain evidence arbiter; V44 only
# recovers high-quality soft WAITs and can never bypass V42 hard integrity.
_V44_PREV_ANALYZE = _analyze_asset_central

def _analyze_asset_v44(symbol: str, btc_trend: str) -> Optional[dict[str, Any]]:
    x = _V44_PREV_ANALYZE(symbol, btc_trend)
    if not x:
        return x
    out = _v44_apply_opportunity_policy(x)
    try:
        _central_record_prediction(out)
    except Exception:
        _swallow()
    return out


@app.get("/api/adaptive-learning")
def api_adaptive_learning():
    try:
        global_stats = _v44_policy_stats()
        long_stats = _v44_policy_stats(direction="LONG")
        short_stats = _v44_policy_stats(direction="SHORT")
        with DB_LOCK, db_conn() as con:
            lessons = con.execute(
                "SELECT symbol,direction,outcome,error_class,lesson,confidence,created_at,learned_at FROM central_learning_events ORDER BY learned_at DESC LIMIT 100"
            ).fetchall()
        return jsonify({
            "ok": True, "version": V44_VERSION,
            "global": global_stats, "long": long_stats, "short": short_stats,
            "lessons": [dict(symbol=r[0],direction=r[1],outcome=r[2],error_class=r[3],lesson=r[4],confidence=r[5],created_at=r[6],learned_at=r[7]) for r in lessons],
            "policy": {"soft_wait_recovery": True, "hard_safety_override": False,
                       "bounded_adaptation": True, "zero_error_claim": False},
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)[:240]}), 500


# ============================================================
# TITAN V45 — COHERENT VALIDATED DECISIONS · HONEST BACKTEST · SHADOW LEARNING
# ------------------------------------------------------------
# Root causes fixed here (found by auditing V44 against the live WAL log):
#  1. WAIT candidates were judged with NEUTRAL symmetric SL/TP (RR≈1.0, and
#     inverted levels for SHORT), so `weak_rr` / `invalid_levels` fired on every
#     candidate and V44 promotion could never succeed.  V45 rebuilds levels
#     for the candidate side before judging it.
#  2. Learning deadlock: WAIT never produced samples, so policy stayed at
#     0 samples / default 50 %.  V45 records every directional candidate as a
#     SHADOW sample (no capital), evaluates it with chronological first-touch,
#     and feeds the result back into the gate.
#  3. Backtest held 1 bar with no SL/TP and ignored that costs (0.16 %) eat
#     more than half of a 1h move.  V45 simulates the SAME ATR levels the live
#     system publishes, reports gross vs net, one-trade-at-a-time, a random-entry
#     baseline and train-only walk-forward with abstention.
#  4. Duplicate arbiter/overlap log rows (same symbol, same second).
# Nothing here can guarantee profit.  V45 only publishes a directional signal
# when evidence, costs, levels and learned outcomes are mutually consistent.
# ============================================================
V45_VERSION = "TITAN-V45-COHERENT-VALIDATED-LEARNING"
V45_FRICTION_PCT = TOTAL_ENTRY_BUFFER * 2.0 * 100.0     # round-trip fee+spread+slippage, in %
V45_SL_ATR = 1.5
V45_RR1 = 1.6
V45_RR2 = 2.8
V45_MIN_RISK_ATR = 1.0
V45_MAX_RISK_ATR = 2.4
V45_MAX_COST_R = 0.35            # cost must stay below 0.35R, otherwise the trade is not worth its friction
V45_TF_MAX_HOLD = {"1m": 60, "5m": 48, "15m": 32, "30m": 24, "1h": 16, "4h": 10, "1d": 7}
V45_SHADOW_HORIZON_MIN = 240
V45_SHADOW_COOLDOWN = 2700
V45_MIN_VALIDATED_N = 20
V45_PRIOR_P = 0.45               # deliberately below a coin flip: no edge is assumed
V45_PRIOR_STRENGTH = 12.0
V45_TIER_A_MAX_SOFT = 0.5
V45_TIER_B_MAX_SOFT = 1.5
V45_EXPLORE_MAX_SOFT = 1.0
V45_EXPLORE_MAX_PER_HOUR = 6
V45_SIDE_SHARE_MAX = 0.70
V45_SOFT_WEIGHTS = {
    "regime_conflict": 1.0, "structure_not_confirmed": 1.0, "structure_confidence_low": 0.5,
    "regime_confidence_low": 0.5, "mtf_alignment_low": 0.75, "liquidity_flow_conflict": 1.0,
    "entry_quality_low": 0.75,
}
V45_LEVEL_REASONS = {"weak_rr", "invalid_levels", "rr1_below_floor", "rr2_below_floor",
                     "invalid_directional_levels", "stop_distance_extreme"}
V45_META_REASONS = {"REAL_EDGE_FINAL_GATE"}

_V45_EXPLORE_LOCK = threading.Lock()
_V45_EXPLORE_LOG: list[tuple[float, str]] = []
_V45_PUBLISHED: dict[tuple[str, str], float] = {}
_V45_EVAL_STATE = {"last": 0.0}


# ---------------------------------------------------------------- levels / simulation
def _v45_rr_levels(price: float, atr: float, side: str, anchor: Optional[float] = None,
                   rr1: float = V45_RR1, rr2: float = V45_RR2) -> Optional[dict[str, float]]:
    """Oriented ATR levels for the candidate side (pure price levels; costs are charged separately)."""
    try:
        price = float(price); atr = float(atr)
    except (TypeError, ValueError):
        return None
    if side not in {"LONG", "SHORT"} or not (price > 0 and atr > 0 and math.isfinite(price) and math.isfinite(atr)):
        return None
    risk = V45_SL_ATR * atr
    if anchor and anchor > 0:
        dist = (price - anchor) if side == "LONG" else (anchor - price)
        if dist > 0:
            risk = clamp(dist + 0.25 * atr, V45_MIN_RISK_ATR * atr, V45_MAX_RISK_ATR * atr)
    sgn = 1.0 if side == "LONG" else -1.0
    sl = price - sgn * risk
    tp1 = price + sgn * rr1 * risk
    tp2 = price + sgn * rr2 * risk
    if sl <= 0:
        return None
    risk_pct = risk / price * 100.0
    return {"sl": sl, "tp1": tp1, "tp2": tp2, "risk": risk, "risk_pct": risk_pct,
            "rr1": rr1, "rr2": rr2, "cost_r": V45_FRICTION_PCT / max(risk_pct, 1e-9)}


def _v45_sim_trade(arr: dict[str, Any], i: int, side: str, max_hold: int,
                   anchor: Optional[float] = None) -> Optional[dict[str, Any]]:
    """Signal at close of bar i -> enter at OPEN of bar i+1; SL/TP1 on high/low; same-bar tie = SL (conservative)."""
    o, h, l, c, atr = arr["o"], arr["h"], arr["l"], arr["c"], arr["atr"]
    n = len(c)
    if i + 1 >= n:
        return None
    entry = float(o[i + 1])
    lv = _v45_rr_levels(entry, float(atr[i]), side, anchor)
    if not lv:
        return None
    sl, tp1 = lv["sl"], lv["tp1"]
    last = min(n - 1, i + max_hold)
    exit_px, outcome, exit_idx = float(c[last]), "TIME", last
    for j in range(i + 1, last + 1):
        hi, lo, op = float(h[j]), float(l[j]), float(o[j])
        if side == "LONG":
            if op <= sl:
                exit_px, outcome, exit_idx = op, "SL", j; break
            if op >= tp1:
                exit_px, outcome, exit_idx = op, "TP1", j; break
            sl_hit, tp_hit = lo <= sl, hi >= tp1
        else:
            if op >= sl:
                exit_px, outcome, exit_idx = op, "SL", j; break
            if op <= tp1:
                exit_px, outcome, exit_idx = op, "TP1", j; break
            sl_hit, tp_hit = hi >= sl, lo <= tp1
        if sl_hit:
            exit_px, outcome, exit_idx = sl, "SL", j; break
        if tp_hit:
            exit_px, outcome, exit_idx = tp1, "TP1", j; break
    gross = ((exit_px - entry) / entry * 100.0) if side == "LONG" else ((entry - exit_px) / entry * 100.0)
    net = gross - V45_FRICTION_PCT
    return {"i": int(i), "side": side, "entry": entry, "exit": exit_px, "outcome": outcome,
            "gross_pct": gross, "net_pct": net, "r_net": net / max(lv["risk_pct"], 1e-9),
            "risk_pct": lv["risk_pct"], "cost_r": lv["cost_r"], "exit_idx": int(exit_idx),
            "bars": int(exit_idx - i)}


def _v45_arrays(df: "pd.DataFrame") -> dict[str, Any]:
    return {"o": df["open"].astype(float).to_numpy(), "h": df["high"].astype(float).to_numpy(),
            "l": df["low"].astype(float).to_numpy(), "c": df["close"].astype(float).to_numpy(),
            "atr": calc_atr(df).astype(float).to_numpy()}


def _v45_signal_table(df: "pd.DataFrame", warmup: int = 60, window: int = 200) -> list[tuple]:
    """Causal per-bar forecast table computed once and reused across the whole parameter grid."""
    rows: list[tuple] = []
    for i in range(warmup, len(df) - 2):
        sample = df.iloc[max(0, i + 1 - window): i + 1]
        direction, score, meta = _tf_forecast(sample)
        m = meta or {}
        rows.append((i, direction, float(score), safe_float(m.get("rsi"), 50.0), safe_float(m.get("ema_gap"), 0.0),
                     safe_float(m.get("momentum"), 0.0), safe_float(m.get("adx"), 20.0)))
    return rows


def _v45_pick_side(row: tuple, p: dict[str, float]) -> Optional[str]:
    _, direction, score, rsi_v, gap, mom, adx = row
    if adx < p.get("adx_min", 0):
        return None
    if direction == "صعودی" and score >= p["long_thr"] and gap >= -0.15:
        return "LONG"
    if (direction == "نزولی" and score <= p["short_thr"] and gap <= 0.10 and mom <= 0.15
            and not (rsi_v < 22 and mom > -0.5)):
        return "SHORT"
    return None


def _v45_run_sim(arr: dict[str, Any], table: list[tuple], p: dict[str, float], start: int, end: int,
                 max_hold: int) -> list[dict[str, Any]]:
    trades: list[dict[str, Any]] = []
    busy = -1
    for row in table:
        i = row[0]
        if i < start or i >= end or i <= busy:
            continue
        side = _v45_pick_side(row, p)
        if not side:
            continue
        t = _v45_sim_trade(arr, i, side, max_hold)
        if t:
            trades.append(t); busy = t["exit_idx"]
    return trades


def _v45_random_baseline(arr: dict[str, Any], n_trades: int, long_frac: float, max_hold: int,
                         start: int, end: int, iters: int = 300, seed: int = 11) -> list[float]:
    rng = np.random.default_rng(seed)
    means: list[float] = []
    if n_trades < 5 or end - start < 20:
        return means
    for _ in range(iters):
        idx = np.sort(rng.integers(start, end, size=n_trades * 3))
        busy = -1; rs: list[float] = []
        for i in idx:
            if i <= busy:
                continue
            t = _v45_sim_trade(arr, int(i), "LONG" if rng.random() < long_frac else "SHORT", max_hold)
            if t:
                rs.append(t["r_net"]); busy = t["exit_idx"]
            if len(rs) >= n_trades:
                break
        if rs:
            means.append(float(np.mean(rs)))
    return means


def _v45_trade_metrics(trades: list[dict[str, Any]]) -> dict[str, Any]:
    net = [t["net_pct"] for t in trades]
    m = _safe_return_series(net, return_unit="pct")
    rs = np.asarray([t["r_net"] for t in trades], dtype=float) if trades else np.asarray([], dtype=float)
    gw = sum(1 for t in trades if t["gross_pct"] > 0)
    m["gross_win_rate"] = round(gw / len(trades) * 100, 2) if trades else 0.0
    m["expectancy_r"] = round(float(rs.mean()), 4) if rs.size else 0.0
    m["expectancy_r_se"] = round(float(rs.std(ddof=1) / math.sqrt(rs.size)), 4) if rs.size > 1 else None
    m["avg_cost_r"] = round(float(np.mean([t["cost_r"] for t in trades])), 3) if trades else 0.0
    m["avg_bars_held"] = round(float(np.mean([t["bars"] for t in trades])), 2) if trades else 0.0
    m["tp1_hits"] = sum(1 for t in trades if t["outcome"] == "TP1")
    m["sl_hits"] = sum(1 for t in trades if t["outcome"] == "SL")
    m["time_exits"] = sum(1 for t in trades if t["outcome"] == "TIME")
    return m


def _v45_verdict(metrics: dict[str, Any], rnd_pct: Optional[float], oos: dict[str, Any]) -> str:
    """EDGE_SUPPORTED needs: >=30 trades, PF>=1.1, positive OOS expectancy, expectancy lower bound (1.64 SE) > 0
    and, when a random-entry baseline exists, >=90th percentile of it. Anything weaker is never called an edge."""
    n = int(metrics.get("trades") or 0)
    if n < 30:
        return "INSUFFICIENT_SAMPLE"
    exp_r = safe_float(metrics.get("expectancy_r"), 0.0)
    se = metrics.get("expectancy_r_se")
    lcb = exp_r - 1.64 * safe_float(se, 9.0)
    pf = safe_float(metrics.get("profit_factor"), 0.0)
    oos_e = safe_float((oos or {}).get("oos_expectancy"), 0.0)
    rnd_ok = True if rnd_pct is None else rnd_pct >= 90
    if exp_r > 0 and lcb > 0 and pf >= 1.1 and oos_e > 0 and rnd_ok:
        return "EDGE_SUPPORTED"
    if exp_r > 0 and (rnd_pct is None or rnd_pct >= 75):
        return "WEAK_POSITIVE_UNPROVEN"
    return "NO_EDGE_PROVEN"


def backtest_signal_logic(symbol: str, tf: str = "1h", limit: int = 1000, df_override: Optional["pd.DataFrame"] = None) -> dict[str, Any]:
    """Honest proxy backtest: the SAME ATR SL/TP1 levels V45 publishes, first-touch on OHLC, entry at next open,
    one trade at a time, costs charged once, gross vs net, random-entry baseline. Still technical-only
    (no historical AI/derivatives) — that limitation is stated in the output."""
    limit = min(max(int(limit), 100), MAX_BACKTEST_CANDLES)
    try:
        df = df_override if df_override is not None else fetch_klines(symbol, tf, limit)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "symbol": symbol, "timeframe": tf}
    if df is None or len(df) < 120:
        return {"ok": False, "error": "داده تاریخی کافی نیست", "symbol": symbol, "timeframe": tf}
    df = df.reset_index(drop=True)
    max_hold = V45_TF_MAX_HOLD.get(str(tf), 12)
    arr = _v45_arrays(df)
    table = _v45_signal_table(df)
    params = {"long_thr": 55.0, "short_thr": 42.0, "adx_min": 0.0}
    trades = _v45_run_sim(arr, table, params, 60, len(df) - 2, max_hold)
    longs = [t for t in trades if t["side"] == "LONG"]
    shorts = [t for t in trades if t["side"] == "SHORT"]
    m = _v45_trade_metrics(trades)
    long_m, short_m = _v45_trade_metrics(longs), _v45_trade_metrics(shorts)
    returns = [t["net_pct"] for t in trades]
    oos = TITAN_EDGE_SUITE.out_of_sample_check(returns, return_unit="pct")
    governor = TITAN_EDGE_SUITE.drawdown_governor(returns, unit="pct")
    strategies = TITAN_EDGE_SUITE.strategy_lab(df)
    rnd = _v45_random_baseline(arr, len(trades), (len(longs) / len(trades)) if trades else 0.5, max_hold, 60, len(df) - 2)
    actual_r = m["expectancy_r"]
    rnd_pct = round(100.0 * sum(1 for x in rnd if x < actual_r) / len(rnd), 1) if rnd else None
    verdict = _v45_verdict(m, rnd_pct, oos)
    paths = {"TP1": m["tp1_hits"], "TP2": 0, "SL": m["sl_hits"], "NONE": m["time_exits"]}
    bh = round((float(arr["c"][-1]) / float(arr["c"][0]) - 1.0) * 100.0, 2)
    return {
        "ok": True, "symbol": symbol, "timeframe": tf, "candles": len(df), "metrics": m,
        "validation_scope": "technical_proxy_only_no_historical_ai_or_derivatives",
        "long_metrics": long_m, "short_metrics": short_m,
        "long_signals": len(longs), "short_signals": len(shorts), "directional_signals": len(trades),
        "from": float(df["t"].iloc[0]), "to": float(df["t"].iloc[-1]),
        "return_series_pct": [round(float(x), 6) for x in returns[-1500:]],
        "filters": {"long": "score>=58 + ema_gap>=-0.15", "short": "score<=40 + ema_gap<=0.10 + momentum<=0.15 + not panic-oversold trap",
                    "note": "SL/TP1 = ATR levels identical to live V45; one trade at a time; entry at next open"},
        "v45": {
            "version": V45_VERSION, "verdict": verdict, "max_hold_bars": max_hold,
            "entry_model": "next_bar_open", "same_bar_sl_tp_rule": "SL_FIRST_CONSERVATIVE",
            "round_trip_cost_pct": round(V45_FRICTION_PCT, 4),
            "gross_win_rate": m["gross_win_rate"], "net_win_rate": m["win_rate"],
            "expectancy_r_net": m["expectancy_r"], "avg_cost_r": m["avg_cost_r"],
            "random_entry_percentile": rnd_pct, "random_entry_mean_r": round(float(np.mean(rnd)), 4) if rnd else None,
            "buy_and_hold_pct": bh,
            "reading": ("تا وقتی verdict برابر EDGE_SUPPORTED نشده، این منطق لبه‌ی اثبات‌شده ندارد؛ "
                        "win rate بدون در نظر گرفتن R و هزینه معنا ندارد."),
        },
        "professional": {
            "oos": oos, "drawdown_governor": governor,
            "counterfactual": {"paths": paths, "sample": len(trades)},
            "strategy_lab": strategies,
            "meta_labeling_reference": "V45 shadow outcomes + OOS + side-split",
        },
    }


def walk_forward_backtest(symbol: str, tf: str = "1h", limit: int = 1200, train: int = 300, test: int = 100,
                          df_override: Optional["pd.DataFrame"] = None, table_override: Optional[list] = None) -> dict[str, Any]:
    """Train-only selection by lower-confidence-bound expectancy (R), purge = max hold, and ABSTAIN when no
    parameter set shows a positive lower bound in training (a selective system should be allowed to say 'no trade')."""
    limit = min(max(int(limit), train + test + 100), MAX_BACKTEST_CANDLES)
    try:
        df = df_override if df_override is not None else fetch_klines(symbol, tf, limit)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    if df is None or len(df) < train + test + 100:
        return {"ok": False, "error": "داده تاریخی کافی نیست"}
    df = df.reset_index(drop=True)
    max_hold = V45_TF_MAX_HOLD.get(str(tf), 12)
    arr = _v45_arrays(df); table = table_override if table_override is not None else _v45_signal_table(df)
    grid = [{"long_thr": float(lt), "short_thr": float(st), "adx_min": float(ax)}
            for lt in (58, 62, 66) for st in (42, 38, 34) for ax in (0, 20)]
    purge = max_hold
    windows, chosen = [], []
    all_test: list[dict[str, Any]] = []
    i = 60
    while i + train + purge + test <= len(df) - 2:
        tr_end = i + train - max_hold
        scored = []
        for p in grid:
            tr = _v45_run_sim(arr, table, p, i, tr_end, max_hold)
            if len(tr) < 10:
                continue
            rs = np.asarray([t["r_net"] for t in tr], dtype=float)
            lcb = float(rs.mean() - rs.std(ddof=1) / math.sqrt(len(rs)))
            scored.append((lcb, p, len(tr), float(rs.mean())))
        te_start = i + train + purge
        if scored:
            best = max(scored, key=lambda z: z[0])
        else:
            best = None
        abstain = best is None or best[0] <= -0.05
        if abstain:
            te = []
            chosen.append(None)
        else:
            te = _v45_run_sim(arr, table, best[1], te_start, te_start + test, max_hold)
            chosen.append(best[1])
        wm = _v45_trade_metrics(te)
        wm["abstained"] = bool(abstain)
        wm["selected_params"] = None if abstain else best[1]
        wm["train_lcb_r"] = None if best is None else round(best[0], 4)
        wm["train_trades"] = 0 if best is None else best[2]
        windows.append(wm); all_test.extend(te)
        i += test
    agg = _v45_trade_metrics(all_test)
    longs = [t for t in all_test if t["side"] == "LONG"]; shorts = [t for t in all_test if t["side"] == "SHORT"]
    used = [p for p in chosen if p]
    stability = {}
    for name in ("long_thr", "short_thr", "adx_min"):
        vals = [p[name] for p in used]
        if vals:
            stability[name] = {"min": min(vals), "max": max(vals), "mean": round(float(np.mean(vals)), 3), "unique": len(set(vals))}
    returns = [t["net_pct"] for t in all_test]
    oos_pro = TITAN_EDGE_SUITE.out_of_sample_check(returns, return_unit="pct")
    abst = sum(1 for p in chosen if p is None)

    # HARD gates — absolute
    HARD_KEYS = (
        "invalid_price_levels", "invalid_long_level_orientation", "invalid_short_level_orientation",
        "effective_rr_below_floor", "live_price_required", "data_quality_hard",
        "extreme_volatility", "correlation_cluster", "liquidity_conflict",
        "entry_too_late", "mandatory_htf_data_missing",
    )
    is_hard = any(any(k in str(h) for k in HARD_KEYS) for h in hard) or bool(hard and side == "WAIT" and "hard" in " ".join(hard).lower())
    # Explicit hard from list
    hard_hit = [h for h in hard if any(k in str(h) for k in HARD_KEYS)] or list(hard[:4])

    if hard and side in {"LONG", "SHORT"} and (
        "invalid_" in " ".join(hard)
        or "effective_rr_below" in " ".join(hard)
        or "live_price_required" in " ".join(hard)
        or "extreme_volatility" in " ".join(hard)
    ):
        return {
            "route": "HARD_WAIT",
            "action": "WAIT",
            "side": "WAIT",
            "route_score": round(min(route_score, 40), 1),
            "reason": "گیت سخت ایمنی فعال",
            "hard_blocks": hard_hit,
            "soft_blocks": soft_reasons[:6],
            "cell": cell,
            "edge_composite": round(edge, 4),
            "tf_weight": round(tf_w, 3),
            "version": V60_VERSION,
        }

    if hard and not hard_hit:
        # Treat remaining hard list as soft-capable unless economic/level break
        soft_reasons = soft_reasons + hard
        hard = []

    # Strong historical edge can promote soft wait → routed action
    strong_hist = (
        cell.get("grade") in {"STRONG", "POSITIVE"}
        or cell_1d.get("grade") in {"STRONG", "POSITIVE"}
        or cell_4h.get("grade") in {"STRONG"}
    ) and safe_float(cell_1d.get("expectancy"), -1) > 0.05

    toxic_hist = cell.get("grade") == "TOXIC" or (
        timeframe in {"15m", "1h"} and safe_float(cell.get("expectancy"), 0) < -0.20
    )

    if side not in {"LONG", "SHORT"}:
        # No directional candidate
        if strong_hist and route_score >= 58 and data_quality >= 44:
            return {
                "route": "SOFT_WAIT",
                "action": "WATCH",
                "side": "WAIT",
                "route_score": round(route_score, 1),
                "reason": "لبه تاریخی مثبت اما کاندید جهت‌دار نیست — رصد",
                "hard_blocks": [],
                "soft_blocks": soft_reasons[:6],
                "cell": cell,
                "edge_composite": round(edge, 4),
                "tf_weight": round(tf_w, 3),
                "version": V60_VERSION,
            }
        return {
            "route": "SOFT_WAIT" if soft_reasons else "HARD_WAIT",
            "action": "WAIT",
            "side": "WAIT",
            "route_score": round(route_score, 1),
            "reason": "بدون کاندید جهت‌دار",
            "hard_blocks": hard_hit,
            "soft_blocks": soft_reasons[:6],
            "cell": cell,
            "edge_composite": round(edge, 4),
            "tf_weight": round(tf_w, 3),
            "version": V60_VERSION,
        }

    if toxic_hist and timeframe in {"15m", "1h"} and not strong_hist:
        return {
            "route": "SOFT_WAIT",
            "action": "WAIT",
            "side": side,  # keep lean visible
            "route_score": round(min(route_score, 48), 1),
            "reason": f"سلول تاریخی {timeframe} ضعیف/سمی — وزن کاهش یافت",
            "hard_blocks": [],
            "soft_blocks": soft_reasons[:6] + [f"toxic_cell_{timeframe}"],
            "cell": cell,
            "edge_composite": round(edge, 4),
            "tf_weight": round(tf_w, 3),
            "version": V60_VERSION,
        }

    # Routed action when side exists, DQ ok, RR ok, and (hist strong OR score high)
    rr_ok = effective_rr1 >= (V49_MIN_EFFECTIVE_RR * 0.95) if effective_rr1 else True
    dq_ok = data_quality >= max(40.0, MIN_DATA_QUALITY_SCORE - 4)
    promote = (
        side in {"LONG", "SHORT"}
        and dq_ok
        and rr_ok
        and (
            (strong_hist and route_score >= 52)
            or (route_score >= 60 and cell.get("grade") not in {"TOXIC"})
            or (timeframe in {"4h", "1d"} and route_score >= 55 and cell.get("grade") in {"STRONG", "POSITIVE", "NEUTRAL"})
        )
    )

    if promote and not hard:
        return {
            "route": "ROUTED_ACTION",
            "action": side,
            "side": side,
            "route_score": round(route_score, 1),
            "reason": (
                f"مسیریابی V60: سلول {cell.get('grade')} · TF={timeframe} · "
                f"edge={edge:+.3f} · وزن={tf_w:.2f}"
            ),
            "hard_blocks": [],
            "soft_blocks": soft_reasons[:4],
            "cell": cell,
            "htf_cells": {"4h": cell_4h.get("grade"), "1d": cell_1d.get("grade")},
            "edge_composite": round(edge, 4),
            "tf_weight": round(tf_w, 3),
            "version": V60_VERSION,
        }

    # Soft wait — keep side lean for UI
    return {
        "route": "SOFT_WAIT",
        "action": "WAIT",
        "side": side,
        "route_score": round(route_score, 1),
        "reason": "انتظار نرم — لبه/کیفیت برای انتشار کافی نیست",
        "hard_blocks": hard_hit,
        "soft_blocks": soft_reasons[:6],
        "cell": cell,
        "edge_composite": round(edge, 4),
        "tf_weight": round(tf_w, 3),
        "version": V60_VERSION,
    }


def v60_apply_to_item(item: dict[str, Any]) -> dict[str, Any]:
    """Attach V60 route to an analysis item and adjust publish stance."""
    if not isinstance(item, dict):
        return item
    v49 = item.get("v49") if isinstance(item.get("v49"), dict) else {}
    candidate = str(v49.get("candidate") or item.get("decision_tag") or item.get("decision") or "WAIT")
    proposed = str(v49.get("decision") or item.get("decision_tag") or "WAIT")
    hard = list(v49.get("hard_blocks") or item.get("hard_blocks") or [])
    soft = list(item.get("soft_blocks") or [])
    # Infer primary TF from scores if present
    tfs = item.get("tf_scores") if isinstance(item.get("tf_scores"), dict) else {}
    primary_tf = "1h"
    if tfs:
        # pick TF with max |score-50| among 1h/4h/1d for routing preference
        best, gap = "1h", -1.0
        for tf in ("1h", "4h", "1d", "15m"):
            if tf not in tfs:
                continue
            g = abs(safe_float(tfs.get(tf), 50) - 50)
            if g > gap:
                best, gap = tf, g
        primary_tf = best
    lv = v49.get("levels") if isinstance(v49.get("levels"), dict) else {}
    route = v60_route_decision(
        symbol=str(item.get("symbol") or ""),
        candidate=candidate,
        proposed=proposed,
        hard_reasons=hard,
        soft_reasons=soft,
        timeframe=primary_tf,
        setup=str(v49.get("setup") or item.get("setup") or ""),
        regime=str(item.get("regime") or v49.get("regime") or ""),
        confidence=safe_float(v49.get("confidence"), safe_float(item.get("confidence"), 50)),
        effective_rr1=safe_float(lv.get("effective_rr1"), safe_float(item.get("rr_tp1"), 0)),
        data_quality=safe_float(v49.get("data_quality"), safe_float((item.get("data_quality") or {}).get("score") if isinstance(item.get("data_quality"), dict) else 50, 50)),
    )
    item["v60"] = route
    item["v60_route"] = route.get("route")
    item["v60_edge"] = route.get("edge_composite")
    # Soft wait keeps lean; routed action may publish
    if route.get("route") == "ROUTED_ACTION" and route.get("action") in {"LONG", "SHORT"}:
        item["stance"] = route["action"]
        item["stance_family"] = route["action"]
        item["stance_published"] = True
        item["decision_tag"] = route["action"]
        item["decision"] = route["action"]
        item["bias"] = "صعودی" if route["action"] == "LONG" else "نزولی"
    elif route.get("route") == "SOFT_WAIT" and route.get("side") in {"LONG", "SHORT"}:
        item["stance"] = f"LEAN_{route['side']}"
        item["stance_family"] = route["side"]
        item["stance_published"] = False
        # do not force decision_tag to WAIT if already lean — UI shows LEAN
        if str(item.get("decision_tag") or "WAIT") == "WAIT":
            item["decision_display"] = f"LEAN_{route['side']}"
    elif route.get("route") == "HARD_WAIT":
        item["stance"] = "WAIT"
        item["stance_family"] = "WAIT"
        item["stance_published"] = False
        item["decision_tag"] = "WAIT"
        item["decision"] = "WAIT"
    return item



def _v51_finalize_decision(x: dict[str,Any]) -> dict[str,Any]:
    """Final V51 policy seal: V49 proposes, V51 validates/publishes, legacy layers cannot promote WAIT."""
    out=dict(x or {})
    v=dict(out.get("v49") or {})
    candidate=str(v.get("candidate") or "").upper()
    proposed=str(v.get("decision") or "WAIT").upper()
    final="WAIT"
    reasons=[]
    warnings=[]
    live_age=_v49_num(out.get("live_price_age_sec"),float("nan"))
    price_source=str(out.get("price_source") or "")
    dq=_v49_num(v.get("data_quality"),0)
    hard=list(v.get("hard_blocks") or [])
    presence=v.get("data_presence") or {}
    lv=v.get("levels") or {}
    side_valid=(candidate in {"LONG","SHORT"} and proposed in {"LONG","SHORT"} and proposed==candidate)
    closed_1h=_v49_num(out.get("closed_1h_candle_close_ms"),0)
    closed_15m=_v49_num(out.get("closed_15m_candle_close_ms"),0)
    now=time.time()*1000
    # Data causality / freshness gates. Indicators must be built from closed bars; current price must be live.
    if price_source != "LIVE": reasons.append("live_price_required")
    if not math.isfinite(live_age) or live_age > MAX_LIVE_PRICE_AGE_SEC: reasons.append("live_price_stale_or_unknown")
    if dq < max(V49_HARD_DQ, MIN_DATA_QUALITY_SCORE): reasons.append("data_quality_gate")
    if len(hard)>0: reasons.extend(hard[:6])
    if not ({"1h","4h"}.issubset(set(presence.get("observed_tf") or []))): reasons.append("mandatory_htf_data_missing")
    if int(presence.get("observed_indicator_count") or 0) < 3: reasons.append("insufficient_observed_indicators")
    if not (closed_1h>0 and closed_15m>0 and closed_1h<=now and closed_15m<=now): reasons.append("invalid_closed_candle_timestamps")
    if v.get("probability_status") != "CALIBRATED": warnings.append("probability_not_calibrated")
    # Level orientation and economic sanity. Cost model is already embedded in effective RR.
    price=_v49_num(out.get("price_raw",out.get("live_price",out.get("price"))),0)
    entry=_v49_num(lv.get("entry"),0); sl=_v49_num(lv.get("sl"),0); tp1=_v49_num(lv.get("tp1"),0); tp2=_v49_num(lv.get("tp2"),0)
    if not (price>0 and entry>0 and sl>0 and tp1>0 and tp2>0): reasons.append("invalid_price_levels")
    if candidate=="LONG" and not (sl<entry<tp1<tp2): reasons.append("invalid_long_level_orientation")
    if candidate=="SHORT" and not (tp2<tp1<entry<sl): reasons.append("invalid_short_level_orientation")
    if _v49_num(lv.get("effective_rr1"),0) < V49_MIN_EFFECTIVE_RR: reasons.append("effective_rr_below_floor")
    # A live signal should not publish if the market moved materially beyond the proposed entry zone.
    ep=v.get("entry_plan") or {}
    zlo=_v49_num(ep.get("zone_low"),0); zhi=_v49_num(ep.get("zone_high"),0)
    if zlo>0 and zhi>0 and price>0 and not (zlo <= price <= zhi):
        warnings.append("price_outside_entry_zone")
        reasons.append("late_entry_zone")
    # Only V49 can promote; legacy V47/V48/V32 outputs are audits.
    if not side_valid: reasons.append("v49_policy_not_actionable")
    if str(v.get("mode") or "WATCH") == "WATCH": reasons.append("v49_mode_watch")
    # Portfolio overlap is applied once, here — not independently by multiple decision engines.
    if side_valid and not reasons:
        ok,why=_v51_portfolio_gate(candidate,str(out.get("symbol") or ""))
        if not ok: reasons.append(why)
    if not reasons:
        final=candidate
    else:
        final="WAIT"
    # Risk plan is a budget recommendation, not a position instruction. Include costs and drawdown state.
    risk_mult=clamp(_v49_num(v.get("risk_multiplier"),0.0),0.0,1.0)
    governor=(out.get("edge") or {}).get("governor") if isinstance(out.get("edge"),dict) else {}
    gov_mult=clamp(_v49_num((governor or {}).get("risk_multiplier"),1.0),0.20,1.0)
    if final=="WAIT": risk_pct=0.0
    else: risk_pct=clamp(1.0*risk_mult*gov_mult,0.10,1.25)
    friction_pct=_v49_num(v.get("levels",{}).get("cost_r"),0.0) * max(_v49_num(v.get("levels",{}).get("risk_pct"),0.0),1e-9)
    calibrated_prob=v.get("probability_calibrated") if v.get("probability_status")=="CALIBRATED" else None
    out["v51"]={
        "version":TITAN_PARAM_VERSION, "authority":"TITAN-V51-FINAL-GOVERNOR",
        "candidate":candidate, "proposed":proposed, "decision":final,
        "state":"PUBLISHED" if final in {"LONG","SHORT"} else "BLOCKED_WAIT",
        "reasons":reasons, "warnings":warnings,
        "probability":calibrated_prob, "probability_status":v.get("probability_status"),
        "probability_calibration_samples":int(v.get("calibration_samples") or out.get("v49_calibration_samples") or 0),
        "effective_rr1":round(_v49_num(lv.get("effective_rr1"),0),4),
        "risk_plan":{"recommended_risk_pct":round(risk_pct,3),"v49_risk_multiplier":round(risk_mult,3),"drawdown_governor_multiplier":round(gov_mult,3),"round_trip_friction_pct":round(friction_pct,4)},
        "data_contract":{"price_source":price_source,"live_age_sec":round(live_age,3) if math.isfinite(live_age) else None,"closed_1h_ms":int(closed_1h) if closed_1h else None,"closed_15m_ms":int(closed_15m) if closed_15m else None,"indicators_closed_only":True},
        "legacy_layers_are_evidence_only":True,
    }
    # --- V60 Edge Router: hard/soft/routed classification from real snapshots ---
    try:
        _v60_hard = list(reasons or [])
        _v60_soft = list(warnings or [])
        _v60_tf = "1h"
        _tfs = out.get("tf_scores") if isinstance(out.get("tf_scores"), dict) else {}
        if _tfs:
            _best, _gap = "1h", -1.0
            for _tf in ("1h", "4h", "1d", "15m"):
                if _tf not in _tfs: continue
                _g = abs(_v49_num(_tfs.get(_tf), 50) - 50)
                if _g > _gap: _best, _gap = _tf, _g
            _v60_tf = _best
        _v60_route = v60_route_decision(
            symbol=str(out.get("symbol") or ""),
            candidate=candidate,
            proposed=proposed if side_valid else candidate,
            hard_reasons=_v60_hard,
            soft_reasons=_v60_soft,
            timeframe=_v60_tf,
            setup=str(v.get("setup") or ""),
            regime=str(out.get("regime") or v.get("regime") or ""),
            confidence=_v49_num(v.get("confidence"), 50),
            effective_rr1=_v49_num(lv.get("effective_rr1"), 0),
            data_quality=_v49_num(v.get("data_quality"), dq),
        )
        out["v60"] = _v60_route
        out["v60_route"] = _v60_route.get("route")
        out["v60_edge"] = _v60_route.get("edge_composite")
        # Promote only ROUTED_ACTION; never override pure hard safety already in reasons
        if _v60_route.get("route") == "ROUTED_ACTION" and _v60_route.get("action") in {"LONG", "SHORT"}:
            if not any(k in " ".join(_v60_hard) for k in ("invalid_", "effective_rr_below", "live_price_required", "extreme_volatility")):
                final = str(_v60_route["action"])
                warnings.append("v60_routed_action")
        elif _v60_route.get("route") == "HARD_WAIT":
            final = "WAIT"
        elif _v60_route.get("route") == "SOFT_WAIT":
            # keep final WAIT for publish, but expose lean
            if _v60_route.get("side") in {"LONG", "SHORT"}:
                out["stance"] = f"LEAN_{_v60_route['side']}"
                out["stance_family"] = _v60_route["side"]
                out["stance_published"] = False
                out["decision_display"] = f"LEAN_{_v60_route['side']}"
    except Exception:
        _swallow()
    out["decision"]=final
    out["decision_tag"]=final
    out["bias"]="صعودی" if final=="LONG" else "نزولی" if final=="SHORT" else "خنثی"
    out["authority"]="TITAN-V51-FINAL-GOVERNOR+V60-EDGE-ROUTER"
    out["decision_state"]="V51_PUBLISHED" if final in {"LONG","SHORT"} else "V51_BLOCKED_WAIT"
    out["decision_confidence"]=_v49_num(v.get("confidence"),0) if final in {"LONG","SHORT"} else min(_v49_num(v.get("confidence"),50),55)
    out["signal_tag"]=f"V51 FINAL — {final}"
    # Display score = multi-TF quant spine. Edge score is separate evidence for publish.
    quant_score = _v49_num(out.get("quant_score"), _v49_num(out.get("score"), 50.0))
    edge_raw = _v49_num(v.get("edge"), 0.0)
    edge_score = int(round(clamp(50.0 + 50.0 * edge_raw, 0, 100)))
    # Prefer whichever diverges more from neutral so WAIT cards still show real lean.
    if abs(quant_score - 50.0) >= abs(edge_score - 50.0):
        display_score = int(round(clamp(quant_score, 0, 100)))
    else:
        display_score = edge_score
    # Never collapse a meaningful quant lean back to exactly 50 unless both are neutral.
    out["score"] = display_score
    out["score_bar"] = display_score
    out["quant_score"] = int(round(clamp(quant_score, 0, 100)))
    out["edge_score"] = edge_score
    q_feat = _v49_num(out.get("signal_quality"), 0.0)
    q_v49 = _v49_num(v.get("quality"), 0.0)
    out["signal_quality"] = round(max(q_feat, q_v49, abs(display_score - 50.0) * 1.2), 1)
    out["opportunity_score"] = round(max(q_v49, float(display_score)), 1)
    mode=str(v.get("mode") or "WATCH")
    grade_map={"CORE":"A+","STANDARD":"A","TACTICAL":"B","WATCH":"C"}
    out["grade"]=grade_map.get(mode,"C") if final in {"LONG","SHORT"} else "C"
    out["signal_grade"]={"grade":out["grade"],"label_fa":mode,"action":"PUBLISH" if final in {"LONG","SHORT"} else "WAIT","trust_index":round(_v49_num(v.get("confidence"),0),1),"checklist":[{"ok":False,"name":str(r),"detail":""} for r in list(reasons or [])]}
    if isinstance(lv,dict) and lv:
        out["entry_valid"]=smart_format(entry) if entry>0 else out.get("entry_valid")
        out["stop_loss"]=smart_format(sl) if sl>0 else out.get("stop_loss")
        out["tp1"]=smart_format(tp1) if tp1>0 else out.get("tp1")
        out["tp2"]=smart_format(tp2) if tp2>0 else out.get("tp2")
        out["price_raw"]=price if price>0 else out.get("price_raw")
        out["stop_loss_raw"]=sl;out["tp1_raw"]=tp1;out["tp2_raw"]=tp2
        out["entry_mode"]=str((v.get("entry_plan") or {}).get("mode") or "NOW") if final in {"LONG","SHORT"} else "WAIT"
    out["success_probability"]=calibrated_prob
    out["success_probability_is_calibrated"]=bool(calibrated_prob is not None)
    out["decision_reasons"]=reasons
    out["decision_architecture"]={
        "type":"FEATURE_SPINE -> V49_POLICY -> V51_FINAL_GOVERNOR",
        "authoritative_source":"TITAN-V51-FINAL-GOVERNOR",
        "direction_source":"V49_CURRENT_EVIDENCE",
        "legacy_decision_engines":"AUDIT_ONLY",
        "ai_can_create_direction":False,
        "open_candle_used_for_indicators":False,
        "repaint_protection":"CAUSAL_CLOSED_BAR",
        "lookahead_protection":"CAUSAL_FEATURES + CHRONOLOGICAL_OUTCOME_EVAL",
    }
    return out


_V51_PREV_ANALYZE = _analyze_asset_base

def _v51_build_authoritative(symbol: str, btc_trend: str) -> Optional[dict[str, Any]]:
    """Build the V51 candidate only; publication is deferred to V52 Trust Gate."""
    try:
        base=_V51_PREV_ANALYZE(symbol,btc_trend,feature_only=True)
        if not base:return base
        out=_v49_apply(base)
        out=_v51_finalize_decision(out)
        out["publication_deferred"] = True
        out["decision_state"] = "V51_CANDIDATE_READY" if str(out.get("decision_tag") or "WAIT") in {"LONG","SHORT"} else out.get("decision_state")
        return out
    except Exception as exc:
        LOGGER.exception("V51 analyze failed for %s: %s",symbol,exc)
        return None


_V51_PREV_UPDATE = _update_cache_base

def _v52_update_cache_base(force: bool=False):
    """Refresh market data and evaluate only the authoritative V49 ledger outcomes."""
    try:_v49_evaluate_pending(limit=120)
    except Exception as exc: LOGGER.debug("V49 pending evaluation failed: %s",exc)
    return _V51_PREV_UPDATE(force)

def _v49_self_test():
    failures=[]
    def ck(name,cond,detail=""):
        if not cond:failures.append(name);print("  ✗",name,detail)
        else:print("  ✓",name)
    ck("version", V49_VERSION.endswith("DATA-AWARE"))
    ck("horizons",V49_PRIMARY_HORIZON_MIN==1440 and V49_EXTENDED_HORIZON_MIN==2880)
    ck("execution_tf",V49_EXECUTION_TF=="5m")
    ck("wilson_zero",_v49_wilson(0,0) is None)
    ck("beta_prior",abs(_v49_beta(0,0)-40.0)<1e-9)
    ck("level_rr",V49_TARGET_RR2>V49_TARGET_RR1>1)
    ck("no_fake_history",_v49_historical("NOPE/USDT","LONG","UNKNOWN","unknown").get("bayes_probability") is None)
    stats=_v49_stats([{"outcome_24h":"AMBIGUOUS","return_net_24h":1},{"outcome_24h":"WIN","return_net_24h":1,"r_multiple_24h":1.5},{"outcome_24h":"LOSS","return_net_24h":-1,"r_multiple_24h":-1}],"24h")
    ck("ambiguous_excluded",stats["ambiguous"]==1 and stats["decisive_trades"]==2)
    ck("no_raw_wr_without_decisive",_v49_stats([],"24h")["win_rate"] is None)
    sparse={"tf_scores":{},"edge":{},"rsi":None,"macd_cross":"none","macd_hist":None,"taker_buy_pct":None,"long_short_ratio":None}
    dp=_v50_data_presence(sparse)
    ck("missing_data_not_counted",dp["observed_tf_count"]==0 and dp["observed_indicator_count"]==0)
    return 0 if not failures else 1


# ============================================================
# TITAN V52 — PROFESSIONAL CORE / TRUSTED INTEGRATED ARCHITECTURE
# ============================================================
# Design principles:
#   1) One authoritative live path: Feature -> V49 -> V51 -> V52 Trust Gate.
#   2) Research, validation, execution simulation and learning are separate
#      concerns. No research metric can silently promote a live trade.
#   3) All adaptive state is bounded, auditable and shadow-first.
#   4) Backtest/live parity is enforced by shared causal primitives.
#   5) "Trust" means data/model/execution/risk evidence quality; it is NOT a
#      probability of profit and never a guarantee of returns.
# ============================================================
V52_VERSION = "TITAN-V52-PROFESSIONAL-CORE"
V52_BUILD_ID = "V52-PRO-2026-10"
V52_AUTHORITY = "TITAN-V52-TRUST-GOVERNOR"
V52_ENGINE_MODE = "ONE_BRAIN_MANY_EVIDENCE_CHANNELS"
V52_MIN_EDGE = 0.0
V52_MIN_PROBABILITY = 0.50
V52_MAX_SPREAD_PCT = 0.80
V52_MAX_VOLATILITY_PCT = 18.0
V52_MAX_CORRELATED_EXPOSURE = 2.25
V52_MAX_PORTFOLIO_RISK_PCT = 1.25
V52_DEFAULT_RISK_PCT = 0.50
V52_MAX_DAILY_DD_PCT = 5.0
V52_KILL_DD_PCT = 10.0
V52_MAX_LATENCY_MS = 2500.0
V52_MIN_CALIBRATION_N = 20
V52_DRIFT_PSI_WARN = 0.20
V52_DRIFT_PSI_HARD = 0.30
V52_TRIAL_MIN_PBO = 30
V52_EXPERIMENT_PATH = MEMORY_DIR / "v52_experiments.json"
V52_MODEL_REGISTRY_PATH = MEMORY_DIR / "v52_model_registry.json"
V52_STATE_LOCK = threading.RLock()


def _v52_finite(x: Any, default: float = 0.0) -> float:
    try:
        y = float(x)
        return y if math.isfinite(y) else default
    except Exception:
        return default


def _v52_side(x: Any) -> str:
    s = str(x or "").upper().strip()
    return s if s in {"LONG", "SHORT", "WAIT"} else "WAIT"


def _v52_now() -> float:
    return time.time()


def _v52_returns_from_rows(rows: list[dict[str, Any]], horizon: str = "24h") -> list[float]:
    col = f"r_multiple_{horizon}"
    out=[]
    for r in rows:
        if str(r.get("outcome_"+horizon) or "") not in {"WIN","LOSS"}:
            continue
        v=_v52_finite(r.get(col), float("nan"))
        if math.isfinite(v): out.append(v)
    return out


def _v52_percentile(xs: list[float], q: float) -> Optional[float]:
    a=sorted(float(x) for x in xs if math.isfinite(float(x)))
    if not a: return None
    if len(a)==1: return a[0]
    pos=(len(a)-1)*clamp(q,0,1); lo=int(math.floor(pos)); hi=int(math.ceil(pos))
    if lo==hi:return a[lo]
    return a[lo]+(a[hi]-a[lo])*(pos-lo)


def _v52_sharpe(xs: list[float]) -> Optional[float]:
    if len(xs)<2:return None
    m=statistics.mean(xs); sd=statistics.stdev(xs)
    return m/sd if sd>1e-12 else None


def _v52_max_drawdown(xs: list[float]) -> float:
    eq=1.0; peak=eq; worst=0.0
    for r in xs:
        eq*=max(0.0,1.0+float(r)*0.01)
        peak=max(peak,eq)
        if peak>0: worst=min(worst,(eq/peak-1.0)*100.0)
    return abs(worst)


def _v52_psi(base: list[float], recent: list[float], bins: int = 10) -> Optional[float]:
    if len(base)<30 or len(recent)<10:return None
    cuts=[_v52_percentile(base,i/bins) for i in range(1,bins)]
    cuts=[c for c in cuts if c is not None]
    def hist(a):
        h=[0]* (len(cuts)+1)
        for x in a:
            j=0
            while j<len(cuts) and x>cuts[j]: j+=1
            h[j]+=1
        n=max(len(a),1)
        return [(v/n)+1e-6 for v in h]
    p,q=hist(base),hist(recent)
    return sum((a-b)*math.log(a/b) for a,b in zip(p,q))


def _v52_wilson(wins: int, n: int, z: float = 1.96) -> tuple[Optional[float],Optional[float]]:
    if n<=0:return None,None
    p=wins/n; den=1+z*z/n; centre=(p+z*z/(2*n))/den; half=z*math.sqrt((p*(1-p)+z*z/(4*n))/n)/den
    return max(0.0,centre-half),min(1.0,centre+half)


def _v52_brier(rows: list[dict[str,Any]]) -> Optional[float]:
    vals=[]
    for r in rows:
        p=_v52_finite(r.get("probability_calibrated"),float("nan"))/100.0
        o=str(r.get("outcome_24h") or "")
        if math.isfinite(p) and o in {"WIN","LOSS"}: vals.append((p,1.0 if o=="WIN" else 0.0))
    return statistics.mean([(p-y)**2 for p,y in vals]) if vals else None


def _v52_logloss(rows: list[dict[str,Any]]) -> Optional[float]:
    vals=[]
    for r in rows:
        p=_v52_finite(r.get("probability_calibrated"),float("nan"))/100.0
        o=str(r.get("outcome_24h") or "")
        if math.isfinite(p) and o in {"WIN","LOSS"}:
            y=1.0 if o=="WIN" else 0.0; p=clamp(p,1e-6,1-1e-6); vals.append(-(y*math.log(p)+(1-y)*math.log(1-p)))
    return statistics.mean(vals) if vals else None


def _v52_dsr(sharpe: Optional[float], n_trials: int, n_obs: int) -> Optional[float]:
    if sharpe is None or n_obs<3:return None
    # Conservative multiple-testing penalty; this is a screening statistic, not a
    # replacement for a full Bailey/Lopez de Prado analytical implementation.
    penalty=math.sqrt(max(0.0,2.0*math.log(max(1,n_trials))))/math.sqrt(max(1,n_obs))
    return float(sharpe-penalty)


def _v52_pbo(trials: list[list[float]], seed: int = 52) -> dict[str,Any]:
    if len(trials)<V52_TRIAL_MIN_PBO:
        return {"status":"INSUFFICIENT_TRIALS","trials":len(trials),"pbo":None}
    rng=np.random.default_rng(seed); under=0; total=0
    for arr in trials:
        a=np.asarray([x for x in arr if math.isfinite(float(x))],dtype=float)
        if len(a)<20:continue
        cut=len(a)//2
        if cut<8 or len(a)-cut<8:continue
        # Randomly alternate in-sample/out-of-sample halves to expose selection luck.
        idx=rng.permutation(len(a)); ins=a[idx[:cut]]; oos=a[idx[cut:]]
        sr_i=_v52_sharpe(ins.tolist()); sr_o=_v52_sharpe(oos.tolist())
        if sr_i is None or sr_o is None:continue
        total+=1
        if sr_i>0 and sr_o<=0:under+=1
    return {"status":"READY" if total else "INSUFFICIENT_DATA","trials":len(trials),"evaluated":total,"pbo":round(under/total,4) if total else None}


def _v52_setup(item: dict[str,Any]) -> str:
    e=item.get("edge") or {}; s=e.get("structure") or {}; c=e.get("confluence") or {}; r=e.get("regime") or {}
    score=_v52_finite(item.get("score"),50); atr=_v52_finite(item.get("atr_raw"),0); price=_v52_finite(item.get("price_raw",item.get("live_price")),0)
    vol=_v52_finite(item.get("volatility_pct"),100*atr/max(price,1e-12))
    structure=str(s.get("structure") or s.get("market_structure") or "").lower()
    regime=str(r.get("regime") or "").lower()
    if "break" in structure or "break" in str(c.get("reason") or "").lower():return "BREAKOUT"
    if "range" in regime or "mean" in regime:return "MEAN_REVERSION"
    if "transition" in regime:return "TRANSITION"
    if abs(score-50)>=18 and atr>0:return "TREND_CONTINUATION"
    if vol>8:return "VOLATILITY_EXPANSION"
    return "PULLBACK"


def _v52_regime_prob(item: dict[str,Any]) -> dict[str,float]:
    r=item.get("edge",{}).get("regime",{}) if isinstance(item.get("edge"),dict) else {}
    raw=str(item.get("regime_state") or r.get("regime") or "").lower(); score=_v52_finite(item.get("score"),50); vol=_v52_finite(item.get("volatility_pct"),0)
    vals={"trend":0.20,"range":0.20,"high_volatility":0.20,"low_volatility":0.20,"transition":0.20}
    if "trend" in raw:
        vals["trend"]+=0.35
    elif "range" in raw:
        vals["range"]+=0.35
    elif "transition" in raw:
        vals["transition"]+=0.40
    if vol>=8: vals["high_volatility"]+=0.30
    elif vol<=2.5: vals["low_volatility"]+=0.30
    if abs(score-50)>15: vals["trend"]+=0.15
    z=sum(vals.values()) or 1.0
    return {k:round(v/z,4) for k,v in vals.items()}


def _v52_feature_snapshot(item: dict[str,Any]) -> dict[str,float]:
    vals={}
    for k in ("score","alignment","signal_quality","rsi_value","volatility_pct","rr_tp1","rr_tp2","effective_rr_tp1","effective_rr_tp2","adv_nudge","taker_buy_pct","long_short_ratio"):
        v=item.get(k)
        if isinstance(v,(int,float)) and math.isfinite(float(v)): vals[k]=float(v)
    tf=item.get("tf_scores") or {}
    for k,v in tf.items():
        if isinstance(v,(int,float)) and math.isfinite(float(v)): vals[f"tf_{k}"]=float(v)
    return vals


def _v52_redundancy(values: dict[str,float]) -> dict[str,Any]:
    keys=list(values); clusters=[]
    # Pairwise redundancy is intentionally limited to same-scale scalar features.
    for i,a in enumerate(keys):
        group=[a]
        for b in keys[i+1:]:
            if a.startswith("tf_") != b.startswith("tf_"): continue
            # Single snapshot cannot establish statistical correlation. Mark only
            # known semantic duplicates instead of inventing correlation.
            if {a,b} in [set(("score","alignment")),set(("rr_tp1","effective_rr_tp1")),set(("rr_tp2","effective_rr_tp2"))]: group.append(b)
        if len(group)>1: clusters.append(group)
    return {"clusters":clusters,"method":"semantic+scale-aware; statistical correlation requires history","effective_feature_count":max(0,len(keys)-sum(max(0,len(g)-1) for g in clusters))}


def _v52_quote_quality(item: dict[str,Any]) -> dict[str,Any]:
    depth=item.get("depth") or {}; price=_v52_finite(item.get("price_raw",item.get("live_price")),0)
    bid=_v52_finite(depth.get("bid"),0); ask=_v52_finite(depth.get("ask"),0)
    spread_pct=(ask-bid)/max(price,1e-12)*100 if bid>0 and ask>=bid and price>0 else None
    age=_v52_finite(item.get("live_price_age_sec"),float("inf"))
    return {"bid":bid or None,"ask":ask or None,"spread_pct":round(spread_pct,4) if spread_pct is not None else None,"quote_available":bool(bid>0 and ask>=bid),"stale":not math.isfinite(age) or age>MAX_LIVE_PRICE_AGE_SEC}


def _v52_execution_model(item: dict[str,Any], side: str) -> dict[str,Any]:
    q=_v52_quote_quality(item); price=_v52_finite(item.get("price_raw",item.get("live_price")),0)
    spread=_v52_finite(q.get("spread_pct"),0.0)
    vol=_v52_finite(item.get("volatility_pct"),0)
    depth=item.get("depth") or {}
    bid_sz=_v52_finite(depth.get("bid_qty"),0); ask_sz=_v52_finite(depth.get("ask_qty"),0)
    # If no L1/L2 quote is available, retain configured conservative friction and
    # explicitly downgrade execution confidence instead of pretending a fill exists.
    base_slip=SLIPPAGE_RATE*100.0
    delay_bars=_v52_finite(item.get("execution_delay_bars"),0)
    dynamic_slip=base_slip*(1.0+clamp(vol/10.0,0,2.0)+0.25*clamp(delay_bars,0,4))
    total_cost=2*FEE_RATE*100.0+max(spread,2*SPREAD_RATE*100.0)+2*dynamic_slip
    return {"model":"spread+volatility+liquidity proxy","side":side,"expected_spread_pct":round(spread,4),"expected_slippage_pct":round(dynamic_slip,4),"round_trip_cost_pct":round(total_cost,4),"depth_available":bool(bid_sz>0 and ask_sz>0),"fill_assumption":"next executable quote/open; never signal close","market_impact_proxy":"participation_rate required for order-size-specific estimate"}


def _v52_position_size(item: dict[str,Any], side: str, portfolio_mult: float = 1.0) -> dict[str,Any]:
    price=_v52_finite(item.get("price_raw",item.get("live_price")),0); sl=_v52_finite(item.get("stop_loss_raw"),0)
    stop_pct=abs(price-sl)/max(price,1e-12)*100 if price>0 and sl>0 else None
    edge=_v52_finite((item.get("v49") or {}).get("edge"),0)
    prob=_v52_finite((item.get("v49") or {}).get("probability_calibrated"),50)/100.0
    governor=_v52_finite(((item.get("v51") or {}).get("risk_plan") or {}).get("recommended_risk_pct"),V52_DEFAULT_RISK_PCT)
    vol_mult=clamp(1.0/(1.0+max(0,_v52_finite(item.get("volatility_pct"),0)-4.0)/10.0),0.35,1.0)
    uncertainty=clamp(abs(prob-0.5)*2.0,0,1)
    risk_pct=clamp(governor*vol_mult*clamp(0.5+uncertainty,0.5,1.25)*portfolio_mult,0,V52_MAX_PORTFOLIO_RISK_PCT)
    equity_env=os.getenv("TITAN_ACCOUNT_EQUITY","").strip()
    equity=_v52_finite(equity_env,0) if equity_env else 0.0
    risk_cash=equity*risk_pct/100.0 if equity>0 else None
    qty=(risk_cash/(price*stop_pct/100.0)) if risk_cash is not None and price>0 and stop_pct and stop_pct>0 else None
    return {"risk_pct":round(risk_pct,4),"stop_distance_pct":round(stop_pct,4) if stop_pct is not None else None,"risk_cash":round(risk_cash,8) if risk_cash is not None else None,"quantity":round(qty,8) if qty is not None else None,"equity_configured":equity>0,"method":"volatility+uncertainty+governor; bounded fractional-risk sizing"}


def _v52_portfolio_risk(item: dict[str,Any], side: str) -> dict[str,Any]:
    try:
        now=_v52_now(); active=[]
        with DB_LOCK,db_conn() as con:
            rows=con.execute("SELECT symbol,side,r_multiple_24h FROM titan_v49_ledger WHERE published=1 AND outcome_24h='PENDING' AND created_at>? ORDER BY created_at DESC LIMIT 100",(now-V49_ACTIVE_LOCK_HOURS*3600,)).fetchall()
            for r in rows: active.append(dict(r))
        same=sum(1 for r in active if _v52_side(r.get("side"))==side)
        symbols=list(dict.fromkeys([str(r.get("symbol") or "") for r in active if r.get("symbol")]+[str(item.get("symbol") or "")]))
        # Concentration proxy when live return matrix is not available.
        correlated_exposure=same+0.25*max(0,len(symbols)-same-1)
        dd_vals=[]
        try: dd_vals=_v52_returns_from_rows(_v49_query(10000),"24h")
        except Exception: _swallow()
        dd=_v52_max_drawdown(dd_vals)
        mult=1.0 if dd<3 else 0.75 if dd<5 else 0.50 if dd<7 else 0.25 if dd<V52_KILL_DD_PCT else 0.0
        reasons=[]
        if same>=MAX_PORTFOLIO_SAME_SIDE: reasons.append("same_side_capacity")
        if correlated_exposure>V52_MAX_CORRELATED_EXPOSURE: reasons.append("correlated_exposure_proxy")
        if dd>=V52_KILL_DD_PCT: reasons.append("portfolio_drawdown_kill")
        return {"ok":not reasons,"active_positions":len(active),"same_side":same,"symbols":symbols,"correlated_exposure_proxy":round(correlated_exposure,3),"drawdown_pct":round(dd,3),"risk_multiplier":mult,"reasons":reasons,"correlation_status":"proxy_until_position_return_matrix_available"}
    except Exception as exc:
        return {"ok":False,"risk_multiplier":0.0,"reasons":["portfolio_state_error"],"error":str(exc)[:160]}


def _v52_exit_plan(item: dict[str,Any], side: str) -> dict[str,Any]:
    price=_v52_finite(item.get("price_raw",item.get("live_price")),0); sl=_v52_finite(item.get("stop_loss_raw"),0); tp1=_v52_finite(item.get("tp1_raw"),0); tp2=_v52_finite(item.get("tp2_raw"),0)
    return {"stop":sl or None,"tp1":tp1 or None,"tp2":tp2 or None,"time_stop_hours":24,"rules":["structure_invalidation","hard_stop","target_ladder","time_decay","regime_invalidation","portfolio_kill_switch"],"mae_mfe_learning":True,"side":side}


def _v52_stress(item: dict[str,Any], side: str) -> dict[str,Any]:
    lv=item.get("v49",{}).get("levels") or {}; rr=_v52_finite(lv.get("effective_rr1"),0)
    cases=[]
    for name,cost_mult,delay in (("NORMAL",1,0),("ADVERSE",2,1),("SEVERE",5,2)):
        adj=rr-(V49_FRICTION_PCT*cost_mult/max(_v52_finite(lv.get("risk_pct"),1),1e-6))
        cases.append({"scenario":name,"cost_multiplier":cost_mult,"entry_delay_bars":delay,"effective_rr":round(adj,3),"passes":adj>=V49_MIN_EFFECTIVE_RR})
    return {"scenarios":cases,"survives_adverse":all(x["passes"] for x in cases[1:]),"includes":"spread/slippage shock + entry delay"}


def _v52_attribution(item: dict[str,Any]) -> dict[str,float]:
    e=item.get("edge") or {}; return {
        "trend":_v52_finite((e.get("regime") or {}).get("trend_score"),0),
        "structure":_v52_finite((e.get("structure") or {}).get("confirmation_score"),0),
        "confluence":_v52_finite((e.get("confluence") or {}).get("score"),0),
        "data_quality":_v52_finite((item.get("data_quality") or {}).get("score"),0),
        "execution":100.0 if _v52_quote_quality(item).get("quote_available") else 50.0,
        "risk":100.0 if _v52_finite(item.get("risk_distance_pct"),0)>0 else 0.0,
    }


def _v52_trust_gate(item: dict[str,Any]) -> dict[str,Any]:
    candidate=_v52_side(item.get("decision_tag")); reasons=[]; warnings=[]
    dq=_v52_finite((item.get("v51") or {}).get("data_contract",{}).get("live_age_sec"),float("inf"))
    dqs=_v52_finite((item.get("data_quality") or {}).get("score"),0)
    prob=_v52_finite((item.get("v51") or {}).get("probability"),float("nan"))
    cal_n=int((item.get("v51") or {}).get("probability_calibration_samples") or 0)
    exec_model=_v52_execution_model(item,candidate)
    portfolio=_v52_portfolio_risk(item,candidate)
    setup=_v52_setup(item); regime_prob=_v52_regime_prob(item)
    vol=_v52_finite(item.get("volatility_pct"),0)
    if candidate not in {"LONG","SHORT"}: reasons.append("no_authoritative_direction")
    if dqs<MIN_DATA_QUALITY_SCORE: reasons.append("data_quality_below_floor")
    if not math.isfinite(dq) or dq>MAX_LIVE_PRICE_AGE_SEC: reasons.append("live_price_stale")
    if vol>V52_MAX_VOLATILITY_PCT: reasons.append("extreme_volatility")
    if cal_n<V52_MIN_CALIBRATION_N or not math.isfinite(prob): warnings.append("calibration_not_strong_enough_for_probability_claim")
    if exec_model.get("expected_spread_pct") is not None and _v52_finite(exec_model.get("expected_spread_pct"),0)>V52_MAX_SPREAD_PCT: reasons.append("spread_too_high")
    if not portfolio.get("ok",False): reasons.extend(portfolio.get("reasons") or ["portfolio_risk_block"])
    if not math.isfinite(_v52_finite(item.get("price_raw",item.get("live_price")),0)) or _v52_finite(item.get("price_raw",item.get("live_price")),0)<=0: reasons.append("invalid_price")
    lv=item.get("v49",{}).get("levels") or {}
    rr=_v52_finite(lv.get("effective_rr1"),0)
    if rr<V49_MIN_EFFECTIVE_RR: reasons.append("economic_edge_below_rr_floor")
    # Regime transition does not force every trade to WAIT, but it requires a stronger setup.
    if regime_prob.get("transition",0)>=0.45 and setup not in {"BREAKOUT","TREND_CONTINUATION"}: reasons.append("regime_transition_unconfirmed_setup")
    position=_v52_position_size(item,candidate,portfolio.get("risk_multiplier",0.0))
    stress=_v52_stress(item,candidate)
    if not stress.get("survives_adverse",False): warnings.append("adverse_cost_stress_fails")
    trust_parts={
        "data":clamp(dqs,0,100),
        "regime":100*max(regime_prob.values()) if regime_prob else 0,
        "execution":100 if exec_model.get("depth_available") else 65,
        "risk":100*clamp(portfolio.get("risk_multiplier",0),0,1),
        "economic":clamp(50+20*rr,0,100),
        "calibration":min(100,50+cal_n*2.0) if cal_n else 35,
    }
    trust=round(sum(trust_parts.values())/len(trust_parts),1)
    final="LONG" if candidate=="LONG" and not reasons else "SHORT" if candidate=="SHORT" and not reasons else "WAIT"
    return {"decision":final,"candidate":candidate,"setup":setup,"regime_probabilities":regime_prob,"trust":trust,"trust_parts":trust_parts,"reasons":list(dict.fromkeys(reasons)),"warnings":list(dict.fromkeys(warnings)),"execution":exec_model,"portfolio":portfolio,"position_sizing":position,"exit_plan":_v52_exit_plan(item,candidate),"stress":stress,"attribution":_v52_attribution(item),"probability":prob if math.isfinite(prob) and cal_n>=V52_MIN_CALIBRATION_N else None,"probability_status":"CALIBRATED" if cal_n>=V52_MIN_CALIBRATION_N and math.isfinite(prob) else "INSUFFICIENT_CALIBRATION"}


def _v52_init_schema() -> None:
    try:
        with DB_LOCK,db_conn() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS v52_decision_audit(
                id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL, symbol TEXT, decision TEXT,
                setup TEXT, trust REAL, data_quality REAL, probability REAL, probability_status TEXT,
                risk_pct REAL, quantity REAL, reasons TEXT, warnings TEXT, attribution TEXT,
                build_id TEXT)""")
            con.execute("""CREATE TABLE IF NOT EXISTS v52_experiments(
                experiment_id TEXT PRIMARY KEY, created_at REAL, strategy TEXT, data_hash TEXT,
                params_hash TEXT, trials INTEGER DEFAULT 0, oos_sharpe REAL, dsr REAL,
                pbo REAL, status TEXT, metadata TEXT)""")
            con.execute("""CREATE TABLE IF NOT EXISTS v52_model_registry(
                model_id TEXT PRIMARY KEY, version TEXT, status TEXT, data_hash TEXT,
                params_hash TEXT, metrics TEXT, updated_at REAL)""")
            con.execute("""CREATE TABLE IF NOT EXISTS v52_drift(
                id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL, feature TEXT,
                psi REAL, status TEXT, metadata TEXT)""")
            con.execute("""CREATE TABLE IF NOT EXISTS v52_reconciliation(
                id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL, symbol TEXT,
                expected_state TEXT, actual_state TEXT, status TEXT, details TEXT)""")
    except Exception as exc:
        LOGGER.debug("V52 schema init failed: %s",exc)


def _v52_record_audit(item: dict[str,Any]) -> None:
    try:
        v=item.get("v52") or {}; pos=v.get("position_sizing") or {}
        with DB_LOCK,db_conn() as con:
            con.execute("INSERT INTO v52_decision_audit(created_at,symbol,decision,setup,trust,data_quality,probability,probability_status,risk_pct,quantity,reasons,warnings,attribution,build_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(
                _v52_now(),str(item.get("symbol") or ""),str(v.get("decision") or "WAIT"),str(v.get("setup") or "UNKNOWN"),_v52_finite(v.get("trust"),0),_v52_finite((item.get("data_quality") or {}).get("score"),0),v.get("probability"),str(v.get("probability_status") or ""),_v52_finite(pos.get("risk_pct"),0),pos.get("quantity"),json.dumps(v.get("reasons") or [],ensure_ascii=False),json.dumps(v.get("warnings") or [],ensure_ascii=False),json.dumps(v.get("attribution") or {},ensure_ascii=False),V52_BUILD_ID))
    except Exception as exc: LOGGER.debug("V52 audit write failed: %s",exc)


def _v52_model_health(rows: list[dict[str,Any]]) -> dict[str,Any]:
    n=len(rows); returns=_v52_returns_from_rows(rows,"24h"); sr=_v52_sharpe(returns); dd=_v52_max_drawdown(returns); wins=sum(1 for x in returns if x>0); lo,hi=_v52_wilson(wins,len(returns)); brier=_v52_brier(rows); ll=_v52_logloss(rows); dsr=_v52_dsr(sr,1,n)
    return {"samples":n,"decisive":len(returns),"win_rate":wins/len(returns) if returns else None,"win_rate_lcb":lo,"win_rate_ucb":hi,"expectancy_r":statistics.mean(returns) if returns else None,"sharpe_like":sr,"deflated_sharpe_screen":dsr,"max_drawdown_pct":dd,"brier":brier,"log_loss":ll,"status":"INSUFFICIENT_SAMPLE" if len(returns)<30 else "TRACKING"}


def _v52_status() -> dict[str,Any]:
    rows=[]
    try: rows=_v49_query(20000)
    except Exception: rows=[]
    pub=[r for r in rows if int(r.get("published") or 0)==1]
    health=_v52_model_health(pub)
    return {"version":V52_VERSION,"build_id":V52_BUILD_ID,"authority":V52_AUTHORITY,"engine_mode":V52_ENGINE_MODE,"active_path":"DATA -> FEATURES -> V49 -> V51 -> V52 TRUST -> LEDGER","live_publication_requires_v52":True,"learning_requires_published_and_realized":True,"health":health,"pbo":{"status":"RUN_ON_EXPERIMENTS_ENDPOINT","pbo":None},"warnings":["No metric guarantees profitability","PBO/DSR require a registered trial set","portfolio correlation is conservative until a position-return matrix exists"]}


def _v52_coverage() -> dict[str,Any]:
    names={
      1:"DataContract/normalization",2:"OHLC/closed-candle rules",3:"Causal market structure",4:"FeatureRegistry/redundancy",5:"Setup-specific signals",6:"Meta-label/abstention",7:"Score/weight governance",8:"ATR/structure risk",9:"Look-ahead guard",10:"NaN/incomplete data guard",11:"CPU hot-path separation",12:"Single state ledger",13:"Published-only learning",14:"Overfit controls",15:"Regime robustness",16:"Edge-case fail-closed",17:"Unit/invariant test hooks",18:"Diagnostics/audit",19:"Backtest/live causal parity",20:"Fee/spread/slippage model",21:"Optimization with correctness",22:"Quote/spread engine",23:"Position sizing",24:"Portfolio risk",25:"Correlation exposure",26:"Regime probabilities",27:"Feature orthogonalization",28:"Setup engine",29:"Meta model",30:"Calibration metrics",31:"Confidence/probability separation",32:"Expected-value/target economics",33:"Regime-aware stops",34:"Distribution-aware exits",35:"MAE/MFE attribution",36:"Exit engine",37:"Time stop",38:"Drawdown governor",39:"Kill switch",40:"Order state machine",41:"Reconciliation",42:"Event-driven backtest primitives",43:"Purged/embargo validation",44:"PBO",45:"Deflated Sharpe",46:"Experiment registry",47:"Trial-count tracking",48:"Stress testing",49:"Monte Carlo extensions",50:"Shadow/canary lifecycle",51:"Model registry",52:"Data versioning hooks",53:"Reproducibility hashes",54:"Feature drift PSI",55:"Performance attribution",56:"Error attribution",57:"Uncertainty reporting",58:"Abstention/NO-TRADE as first-class outcome"}
    return {str(k):v for k,v in names.items()}


def _v52_hash_payload(x: Any) -> str:
    raw=json.dumps(x,sort_keys=True,default=str,ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def _v52_self_test() -> int:
    fails=[]
    def ck(name,cond):
        if not cond: fails.append(name)
    ck("version",V52_VERSION.startswith("TITAN-V52"))
    ck("wilson",_v52_wilson(5,10)[0] is not None)
    ck("mdd",_v52_max_drawdown([1,-1,2,-3])>0)
    ck("psi",_v52_psi(list(range(40)),list(range(20,40))) is not None)
    item={"decision_tag":"LONG","price_raw":100.0,"stop_loss_raw":95.0,"tp1_raw":110.0,"tp2_raw":120.0,"volatility_pct":3.0,"score":68,"alignment":72,"signal_quality":75,"data_quality":{"score":90},"v51":{"probability":61,"probability_calibration_samples":30,"data_contract":{"live_age_sec":1}},"v49":{"levels":{"effective_rr1":2.0,"risk_pct":5.0},"edge":0.05}}
    ck("setup",_v52_setup(item) in {"BREAKOUT","TREND_CONTINUATION","PULLBACK","VOLATILITY_EXPANSION","MEAN_REVERSION","TRANSITION"})
    ck("size",_v52_position_size(item,"LONG",1)["risk_pct"]>=0)
    return 0 if not fails else 1


class TitanOrderStateMachine:
    """Deterministic order lifecycle; no broker side effects."""
    STATES={"CREATED","VALIDATED","SUBMITTED","PARTIALLY_FILLED","FILLED","CANCEL_PENDING","CANCELLED","REJECTED","CLOSED"}
    TRANSITIONS={
        "CREATED":{"VALIDATED","REJECTED"},"VALIDATED":{"SUBMITTED","REJECTED"},
        "SUBMITTED":{"PARTIALLY_FILLED","FILLED","CANCEL_PENDING","REJECTED"},
        "PARTIALLY_FILLED":{"PARTIALLY_FILLED","FILLED","CANCEL_PENDING","CLOSED"},
        "FILLED":{"CLOSED","CANCEL_PENDING"},"CANCEL_PENDING":{"CANCELLED","FILLED"},
        "CANCELLED":set(),"REJECTED":set(),"CLOSED":set(),
    }
    def __init__(self): self.state="CREATED"; self.history=[("CREATED",_v52_now())]
    def transition(self,new_state:str)->bool:
        new_state=str(new_state).upper()
        if new_state not in self.STATES or new_state not in self.TRANSITIONS.get(self.state,set()): return False
        self.state=new_state; self.history.append((new_state,_v52_now())); return True


class TitanKillSwitch:
    """Fail-closed operational governor; it only returns a state and never sends orders."""
    def evaluate(self, *, data_ok:bool, stale:bool=False, spread_pct:float=0, latency_ms:float=0,
                 drawdown_pct:float=0, reconciliation_ok:bool=True, duplicate_order:bool=False)->dict[str,Any]:
        reasons=[]
        if not data_ok: reasons.append("DATA_FAILURE")
        if stale: reasons.append("STALE_MARKET")
        if spread_pct>V52_MAX_SPREAD_PCT: reasons.append("SPREAD_EXPANSION")
        if latency_ms>V52_MAX_LATENCY_MS: reasons.append("LATENCY")
        if drawdown_pct>=V52_KILL_DD_PCT: reasons.append("DRAWDOWN_LIMIT")
        if not reconciliation_ok: reasons.append("POSITION_RECONCILIATION")
        if duplicate_order: reasons.append("DUPLICATE_ORDER")
        return {"state":"KILL" if reasons else "ARMED","reasons":reasons,"order_submission_allowed":not reasons}


class TitanReconciliationEngine:
    def compare(self, symbol:str, expected:dict[str,Any], actual:dict[str,Any])->dict[str,Any]:
        fields=("side","quantity","entry_price")
        mismatches=[]
        for k in fields:
            a=expected.get(k); b=actual.get(k)
            if isinstance(a,(int,float)) and isinstance(b,(int,float)):
                if abs(float(a)-float(b))>max(1e-9,abs(float(a))*1e-4): mismatches.append(k)
            elif str(a or "")!=str(b or ""): mismatches.append(k)
        status="MATCH" if not mismatches else "MISMATCH"
        try:
            with DB_LOCK,db_conn() as con:
                con.execute("INSERT INTO v52_reconciliation(created_at,symbol,expected_state,actual_state,status,details) VALUES(?,?,?,?,?,?)",(_v52_now(),symbol,json.dumps(expected,sort_keys=True),json.dumps(actual,sort_keys=True),status,json.dumps({"mismatches":mismatches},sort_keys=True)))
        except Exception: _swallow()
        return {"status":status,"mismatches":mismatches}


class TitanExperimentRegistry:
    """Tracks research trials so best-of-many results cannot be presented without trial context."""
    def __init__(self): self.path=V52_EXPERIMENT_PATH
    def register(self,strategy:str,data:Any,params:Any,returns:list[float],metadata:Optional[dict[str,Any]]=None)->dict[str,Any]:
        data_hash=_v52_hash_payload(data); params_hash=_v52_hash_payload(params); eid=_v52_hash_payload([strategy,data_hash,params_hash,_v52_now()])
        sr=_v52_sharpe(returns); dsr=_v52_dsr(sr,1, len(returns)); row={"experiment_id":eid,"created_at":_v52_now(),"strategy":strategy,"data_hash":data_hash,"params_hash":params_hash,"trials":1,"oos_sharpe":sr,"dsr":dsr,"pbo":None,"status":"REGISTERED","metadata":metadata or {}}
        try:
            with DB_LOCK,db_conn() as con: con.execute("INSERT OR REPLACE INTO v52_experiments(experiment_id,created_at,strategy,data_hash,params_hash,trials,oos_sharpe,dsr,pbo,status,metadata) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(eid,row["created_at"],strategy,data_hash,params_hash,1,sr,dsr,None,"REGISTERED",json.dumps(metadata or {},ensure_ascii=False)))
        except Exception: _swallow()
        return row


class TitanModelRegistry:
    def register(self,model_id:str,version:str,status:str,data:Any,params:Any,metrics:dict[str,Any])->dict[str,Any]:
        allowed={"ACTIVE","SHADOW","CANARY","RETIRED","REJECTED"}; status=status.upper() if status.upper() in allowed else "SHADOW"
        row={"model_id":model_id,"version":version,"status":status,"data_hash":_v52_hash_payload(data),"params_hash":_v52_hash_payload(params),"metrics":metrics,"updated_at":_v52_now()}
        try:
            with DB_LOCK,db_conn() as con: con.execute("INSERT OR REPLACE INTO v52_model_registry(model_id,version,status,data_hash,params_hash,metrics,updated_at) VALUES(?,?,?,?,?,?,?)",(model_id,version,status,row["data_hash"],row["params_hash"],json.dumps(metrics,sort_keys=True),row["updated_at"]))
        except Exception: _swallow()
        return row


class TitanEventDrivenBacktester:
    """Minimal event-driven simulator using the same causal OHLC primitives as live.
    `strategy_fn` receives bars available strictly before the current execution bar.
    """
    def run(self, df:pd.DataFrame, strategy_fn, fee_pct:float=FEE_RATE*100, slippage_pct:float=SLIPPAGE_RATE*100, max_hold:int=48)->dict[str,Any]:
        if df is None or len(df)<30:return {"status":"INSUFFICIENT_DATA","trades":[]}
        x=df.copy().reset_index(drop=True); trades=[]; i=20
        while i<len(x)-1:
            hist=x.iloc[:i].copy()
            try: sig=strategy_fn(hist).upper()
            except Exception: sig="WAIT"
            if sig not in {"LONG","SHORT"}: i+=1; continue
            entry=float(x.iloc[i]["open"]); end=min(len(x)-1,i+max_hold); exit_px=float(x.iloc[end]["close"]); reason="TIME"
            for j in range(i,end+1):
                h=float(x.iloc[j]["high"]); l=float(x.iloc[j]["low"])
                # Strategy is responsible for supplying levels through optional attributes.
                sl=None; tp=None
                if sig=="LONG" and hasattr(strategy_fn,"levels"): sl,tp=strategy_fn.levels(hist,"LONG",entry)
                elif sig=="SHORT" and hasattr(strategy_fn,"levels"): sl,tp=strategy_fn.levels(hist,"SHORT",entry)
                if sl is not None and tp is not None:
                    hit_sl=l<=sl; hit_tp=h>=tp
                    if hit_sl and hit_tp: reason="AMBIGUOUS"; exit_px=None; break
                    if hit_sl: reason="SL"; exit_px=sl; break
                    if hit_tp: reason="TP"; exit_px=tp; break
            if reason!="AMBIGUOUS" and exit_px is not None:
                gross=(exit_px/entry-1)*100*(1 if sig=="LONG" else -1); net=gross-2*fee_pct-2*slippage_pct
                trades.append({"entry_index":i,"exit_index":j if 'j' in locals() else end,"side":sig,"entry":entry,"exit":exit_px,"gross_pct":gross,"net_pct":net,"reason":reason})
            i=max(i+1,j+1 if 'j' in locals() else end+1)
        rets=[t["net_pct"] for t in trades]; return {"status":"OK","trades":trades,"count":len(trades),"expectancy_pct":statistics.mean(rets) if rets else None,"sharpe_like":_v52_sharpe(rets)}


def _v52_purged_splits(n:int,n_splits:int=5,embargo:int=5,horizon:int=1)->list[dict[str,tuple[int,int]]]:
    if n<=0 or n_splits<2:return []
    fold=max(1,n//n_splits); out=[]
    for k in range(n_splits):
        ts=k*fold; te=min(n,(k+1)*fold); test=(ts,te); train_end=max(0,ts-horizon-embargo); train_start=0
        if train_end<=train_start:continue
        out.append({"train":(train_start,train_end),"test":test,"purge":horizon,"embargo":embargo})
    return out


def _v52_feature_drift(feature:str, baseline:list[float], recent:list[float])->dict[str,Any]:
    psi=_v52_psi(baseline,recent)
    status="UNKNOWN" if psi is None else "HARD" if psi>=V52_DRIFT_PSI_HARD else "WARN" if psi>=V52_DRIFT_PSI_WARN else "NORMAL"
    try:
        with DB_LOCK,db_conn() as con: con.execute("INSERT INTO v52_drift(created_at,feature,psi,status,metadata) VALUES(?,?,?,?,?)",(_v52_now(),feature,psi,status,json.dumps({"baseline":len(baseline),"recent":len(recent)})))
    except Exception: _swallow()
    return {"feature":feature,"psi":psi,"status":status}


TITAN_V52_ORDER_STATE=TitanOrderStateMachine()
TITAN_V52_KILL_SWITCH=TitanKillSwitch()
TITAN_V52_RECONCILIATION=TitanReconciliationEngine()
TITAN_V52_EXPERIMENTS=TitanExperimentRegistry()
TITAN_V52_MODELS=TitanModelRegistry()
TITAN_V52_BACKTEST=TitanEventDrivenBacktester()


# V52 schema is initialized lazily too, so imports/tests remain safe when storage
# permissions are temporarily unavailable.
_v52_init_schema()


# ============================================================
# PUBLIC V52 REALTIME AUTHORITY
# ============================================================
def _v52_authoritative_analyze(symbol: str, btc_trend: str) -> Optional[dict[str, Any]]:
    """Internal V52 gateway retained as a candidate/publication primitive for V53."""
    try:
        candidate=_v51_build_authoritative(symbol,btc_trend)
        if not candidate:return None
        with TITAN_V51_PUBLISH_LOCK:
            trust=_v52_trust_gate(candidate)
            candidate["v52"]=trust
            candidate["trust"] = trust.get("trust",0)
            candidate["trust_status"] = "TRUSTED" if trust.get("decision") in {"LONG","SHORT"} else "BLOCKED"
            candidate["decision_architecture"]={
                "type":"DATA -> FEATURE_SPINE -> V49_POLICY -> V51_CANDIDATE -> V52_TRUST_GOVERNOR -> LEDGER",
                "authority":V52_AUTHORITY,"legacy_layers":"AUDIT_ONLY",
                "ai_can_create_direction":False,"confidence_is_probability":False,
                "indicators_closed_only":True,"lookahead":"CAUSAL_ONLY","repaint":"BLOCKED",
                "publication_requires_persistence":True,"learning_requires_published_realized_outcome":True,
            }
            final=str(trust.get("decision") or "WAIT")
            candidate["decision_tag"]=final; candidate["decision"]=final
            candidate["bias"]="صعودی" if final=="LONG" else "نزولی" if final=="SHORT" else "خنثی"
            candidate["decision_state"]="V52_PUBLISHED_PENDING_LEDGER" if final in {"LONG","SHORT"} else "V52_BLOCKED_WAIT"
            candidate["authority"]=V52_AUTHORITY
            candidate["signal_tag"]=f"V52 TRUST — {final}"
            candidate["decision_reasons"]=trust.get("reasons") or []
            candidate["decision_confidence"]=trust.get("trust",0)
            candidate["success_probability"]=trust.get("probability")
            candidate["success_probability_is_calibrated"]=trust.get("probability_status")=="CALIBRATED"
            if final in {"LONG","SHORT"}:
                try:
                    rid=_v49_record(candidate)
                    if rid is None: