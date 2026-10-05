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
# journals, history and secrets are HARD-LOCKED to Internal Storage/KKK.
# Nothing is written anywhere else on the phone.
# ============================================================

# TITAN storage is HARD-LOCKED to one user-visible folder on Android shared
# internal storage. Nothing is written outside this root by TITAN.
#
# Required layout (all contained inside one folder):
#   /storage/emulated/0/KKK/
#       data/              -> database, settings, logs, replay, flask instance
#       cache/             -> market + AI caches, history, tmp, pycache
#       memory/            -> learning/adaptive memory
#       secrets/           -> optional API-key files
#
# There is intentionally NO fallback to the script directory, current working
# directory, /tmp, /data/data, Android app cache, a desktop profile,
# TITAN_HOME, SD card, or any other path. Only KKK.
TITAN_FOLDER_NAME = "KKK"
TITAN_SHARED_STORAGE = Path("/storage/emulated/0")
TITAN_ANDROID_HOME = TITAN_SHARED_STORAGE / TITAN_FOLDER_NAME

def _select_app_home() -> Path:
    """Return the only allowed TITAN persistent root: Internal Storage/KKK.

    Hard-locked to folder KKK on shared internal storage. Android 15 may expose
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
                # FIX: probe inside KKK itself so nothing is ever written outside the KKK folder.
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
            "TITAN requires writable Internal Storage (KKK). "
            "Grant All-files / storage permission, create folder KKK, then run again. "
            "Nothing is stored outside Internal Storage/KKK."
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

# Force every OS / Python / SQLite / XDG temp+cache write into KKK.
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
    """Verify that every persistent TITAN root is physically inside KKK."""
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
            raise RuntimeError(f"Persistent path escaped KKK root: {path}")
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
TITAN_PARAM_VERSION = "V60.0-ANDROID-KKK-EDGE-ROUTER"
TITAN_BUILD_ID = "V60.0-PRO-2026-10-ANDROID-KKK-EDGE-ROUTER"
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
        raise RuntimeError(f"TITAN path escaped KKK root: {candidate}")
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