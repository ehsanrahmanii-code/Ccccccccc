def fetch_klines(symbol: str, tf: str, limit: int = 150, start_ms: Optional[int] = None, end_ms: Optional[int] = None) -> pd.DataFrame:
    """Fetch closed OHLCV with short TTL memory cache + fast numeric path.

    Cache key includes symbol/tf/limit/range so historical backtests stay uncached
    when start/end are set; live dashboard hits are almost free within TTL.
    """
    limit = min(max(int(limit), 1), 1000)
    cacheable = start_ms is None and end_ms is None
    ckey = _kline_cache_key(symbol, tf, limit, start_ms, end_ms) if cacheable else ""
    if cacheable:
        hit = _kline_cache_get(ckey)
        if hit is not None and len(hit) >= 10:
            return hit

    params: dict[str, Any] = {"symbol": _binance_symbol(symbol), "interval": tf, "limit": limit}
    if start_ms is not None:
        params["startTime"] = int(start_ms)
    if end_ms is not None:
        params["endTime"] = int(end_ms)
    raw = None
    last_err = None
    # Prefer data-api first; shorter timeout on failover hosts for snappy mobile UX
    hosts = KLINES_URLS if "KLINES_URLS" in globals() else ["https://data-api.binance.vision/api/v3/klines"]
    for i, kurl in enumerate(hosts):
        try:
            to = 5 if i == 0 else 3
            response = _http_session().get(kurl, params=params, timeout=to)
            if not response.ok:
                last_err = RuntimeError(f"HTTP {response.status_code} from {kurl}")
                continue
            raw = response.json()
            break
        except Exception as exc:
            last_err = exc
            LOGGER.debug("klines fail %s: %s", kurl, exc)
    if raw is None:
        raise RuntimeError(f"All kline hosts failed: {last_err}")
    if not isinstance(raw, list) or not raw:
        raise RuntimeError("Invalid Binance kline response")

    # Fast path: build from list-of-lists without per-column to_numeric loops
    # Columns: 0=t 1=o 2=h 3=l 4=c 5=v 6=T
    try:
        arr = np.asarray(raw, dtype=object)
        t = arr[:, 0].astype(np.float64)
        o = arr[:, 1].astype(np.float64)
        h = arr[:, 2].astype(np.float64)
        l = arr[:, 3].astype(np.float64)
        c = arr[:, 4].astype(np.float64)
        v = arr[:, 5].astype(np.float64)
        T = arr[:, 6].astype(np.float64)
        now_ms = time.time() * 1000.0
        mask = (
            (T < now_ms) & (t > 0) & (T >= t) & (v >= 0)
            & np.isfinite(t) & np.isfinite(T)
            & np.isfinite(o) & np.isfinite(h) & np.isfinite(l) & np.isfinite(c) & np.isfinite(v)
            & (o > 0) & (h > 0) & (l > 0) & (c > 0)
            & (h >= np.maximum(o, c)) & (l <= np.minimum(o, c)) & (h >= l)
        )
        t, o, h, l, c, v, T = t[mask], o[mask], h[mask], l[mask], c[mask], v[mask], T[mask]
        # sort + dedupe by t (keep last)
        order = np.argsort(t, kind="mergesort")
        t, o, h, l, c, v, T = t[order], o[order], h[order], l[order], c[order], v[order], T[order]
        if len(t) > 1:
            keep = np.ones(len(t), dtype=bool)
            keep[:-1] = t[:-1] != t[1:]
            t, o, h, l, c, v, T = t[keep], o[keep], h[keep], l[keep], c[keep], v[keep], T[keep]
        df = pd.DataFrame({"t": t, "open": o, "high": h, "low": l, "close": c, "vol": v, "T": T})
    except Exception:
        columns = ["t", "open", "high", "low", "close", "vol", "T", "qav", "n", "tbb", "tbq", "ign"]
        df = pd.DataFrame(raw, columns=columns)
        for col in ["open", "high", "low", "close", "vol", "t", "T"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["t", "T", "open", "high", "low", "close", "vol"])
        now_ms = int(time.time() * 1000)
        df = df[
            (df["T"] < now_ms) & (df["t"] > 0) & (df["T"] >= df["t"])
            & (df["vol"] >= 0) & (df[["open", "high", "low", "close"]] > 0).all(axis=1)
            & (df["high"] >= df[["open", "close"]].max(axis=1))
            & (df["low"] <= df[["open", "close"]].min(axis=1)) & (df["high"] >= df["low"])
        ].sort_values("t").drop_duplicates(subset=["t"], keep="last").reset_index(drop=True)

    if len(df) < 10:
        raise RuntimeError(f"Insufficient closed candles for {symbol} {tf}")
    if cacheable:
        _kline_cache_put(ckey, df)
    return df


def _binance_futures_funding(symbol: str) -> Optional[float]:
    try:
        r = _http_session().get(
            "https://fapi.binance.com/fapi/v1/premiumIndex",
            params={"symbol": _binance_symbol(symbol)}, timeout=8,
        )
        if not r.ok:
            return None
        payload = r.json()
        if not isinstance(payload, dict):
            return None
        return safe_float(payload.get("lastFundingRate")) * 100
    except Exception:
        return None


def _binance_futures_oi(symbol: str) -> Optional[float]:
    try:
        r = _http_session().get(
            "https://fapi.binance.com/fapi/v1/openInterest",
            params={"symbol": _binance_symbol(symbol)}, timeout=8,
        )
        if not r.ok:
            return None
        payload = r.json()
        if not isinstance(payload, dict):
            return None
        value = safe_float(payload.get("openInterest"))
        return value if value > 0 else None
    except Exception:
        return None


def fetch_derivatives(symbol: str) -> dict[str, Any]:
    # Short TTL cache — funding/OI rarely need sub-20s refresh during one scan
    _sym_key = _normalize_symbol(symbol)
    with KLINE_CACHE_LOCK:
        _hit = DERIV_CACHE.get(_sym_key)
        if _hit and (time.time() - _hit[0]) < DERIV_CACHE_TTL:
            return _hit[1]
    clean = _binance_symbol(symbol)
    base = clean.replace("USDT", "")
    raw_oi = 0.0
    oi_kind = "unknown"
    funding: Optional[float] = None
    cg_usd = cg_qty = cg_delta = None
    source_parts: list[str] = []

    if COINGLASS_API_KEY:
        headers = {"CG-API-KEY": COINGLASS_API_KEY}
        try:
            res = _http_session().get(
                "https://open-api-v4.coinglass.com/api/futures/open-interest/exchange-list",
                params={"symbol": base}, headers=headers, timeout=10,
            ).json()
            if str(res.get("code")) in {"0", "200"}:
                rows = res.get("data") or []
                row = next((r for r in rows if str(r.get("exchange", "")).lower() == "all"), rows[0] if rows else None)
                if row:
                    cg_qty = safe_float(row.get("open_interest_quantity"), 0)
                    cg_usd = safe_float(row.get("open_interest_usd"), 0)
                    cg_delta = safe_float(row.get("open_interest_change_percent_5m"), 0)
                    if cg_usd and cg_usd > 0:
                        raw_oi, oi_kind = cg_usd, "usd"
                    elif cg_qty and cg_qty > 0:
                        raw_oi, oi_kind = cg_qty, "base"
                    if raw_oi > 0:
                        source_parts.append("CoinGlass-OI")
        except Exception as exc:
            LOGGER.warning("CoinGlass OI failed for %s: %s", symbol, exc)
        try:
            res = _http_session().get(
                "https://open-api-v4.coinglass.com/api/futures/funding-rate/exchange-list",
                params={"symbol": base}, headers=headers, timeout=10,
            ).json()
            if str(res.get("code")) in {"0", "200"}:
                rows = res.get("data") or []
                row = next((r for r in rows if str(r.get("symbol", "")).upper() == base), rows[0] if rows else None)
                if row:
                    vals = [safe_float(x.get("funding_rate"), np.nan) for x in (row.get("stablecoin_margin_list") or [])]
                    vals = [x for x in vals if np.isfinite(x)]
                    if vals:
                        funding = float(np.mean(vals)) * 100
                        source_parts.append("CoinGlass-Funding")
        except Exception as exc:
            LOGGER.warning("CoinGlass funding failed for %s: %s", symbol, exc)

    if funding is None:
        funding = _binance_futures_funding(symbol)
        if funding is not None:
            source_parts.append("Binance-Funding")
    if raw_oi <= 0:
        fallback_oi = _binance_futures_oi(symbol)
        if fallback_oi is not None:
            raw_oi, oi_kind = fallback_oi, "base"
            source_parts.append("Binance-OI")

    now = time.time()
    delta = cg_delta if cg_delta is not None else 0.0
    with OI_LOCK:
        previous = OI_HISTORY.get(symbol)
        if cg_delta is None and previous and previous[2] == oi_kind:
            prev_oi, prev_ts, _ = previous
            if prev_oi > 0 and raw_oi > 0 and now - prev_ts <= 900:
                delta = ((raw_oi - prev_oi) / prev_oi) * 100
        if raw_oi > 0:
            OI_HISTORY[symbol] = (raw_oi, now, oi_kind)

    if cg_usd and cg_usd > 0:
        oi_disp = f"${smart_format(cg_usd)}"
    elif cg_qty and cg_qty > 0:
        oi_disp = f"{smart_format(cg_qty)} {base}"
    elif raw_oi > 0:
        oi_disp = f"{smart_format(raw_oi)} {base}"
    else:
        oi_disp = "N/A"
    ls = fetch_binance_long_short_ratio(symbol)
    taker = fetch_binance_taker_buy_sell(symbol)
    if ls.get("source") and ls.get("source") != "N/A":
        source_parts.append(str(ls.get("source")))
    if taker.get("source") and taker.get("source") != "N/A":
        source_parts.append(str(taker.get("source")))
    out = {
        "oi": oi_disp,
        "raw_oi": raw_oi,
        "oi_delta": delta,
        "funding_value": funding,
        "funding": f"{funding:+.4f}%" if funding is not None else "N/A",
        "source": " + ".join(dict.fromkeys(source_parts)) if source_parts else "N/A",
        "long_short": ls,
        "taker": taker,
        "long_ratio": ls.get("long_ratio"),
        "short_ratio": ls.get("short_ratio"),
        "long_short_ratio": ls.get("long_short_ratio"),
        "top_long_ratio": ls.get("top_long_ratio"),
        "taker_buy_pct": taker.get("buy_pct"),
        "taker_sell_pct": taker.get("sell_pct"),
        "taker_available": bool(taker.get("available")),
    }
    with KLINE_CACHE_LOCK:
        DERIV_CACHE[_normalize_symbol(symbol)] = (time.time(), out)
        if len(DERIV_CACHE) > 80:
            for k, _ in sorted(DERIV_CACHE.items(), key=lambda x: x[1][0])[:20]:
                DERIV_CACHE.pop(k, None)
    return out


def fetch_btc_trend() -> str:
    try:
        df = fetch_klines("BTC/USDT", "1h", 60)
        ema = df["close"].ewm(span=20, adjust=False).mean().iloc[-1]
        return "صعودی" if float(df["close"].iloc[-1]) >= float(ema) else "نزولی"
    except Exception as exc:
        LOGGER.warning("BTC trend failed: %s", exc)
        return "خنثی"


def fetch_macro() -> dict[str, Any]:
    fear_value = "N/A"
    fear_text = "داده نیست"
    dominance = "N/A"
    sources: list[str] = []
    try:
        payload = _http_session().get("https://api.alternative.me/fng/", params={"limit": 1}, timeout=8).json()
        data = payload.get("data") or []
        if data:
            fear_value = data[0].get("value", "N/A")
            fear_text = data[0].get("value_classification", "N/A")
            sources.append("Alternative.me")
    except Exception as exc:
        LOGGER.warning("Fear & Greed failed: %s", exc)
    try:
        payload = _http_session().get("https://api.coingecko.com/api/v3/global", timeout=8).json()
        btc_dom = safe_float((payload.get("data") or {}).get("market_cap_percentage", {}).get("btc"), np.nan)
        if np.isfinite(btc_dom):
            dominance = f"{btc_dom:.2f}%"
            sources.append("CoinGecko")
    except Exception as exc:
        LOGGER.warning("CoinGecko macro failed: %s", exc)
    return {"fear_greed_val": fear_value, "fear_greed_text": fear_text, "global_status": " / ".join(sources) or "N/A", "dominance_btc": dominance}

# ============================================================
# TECHNICALS / DECISION CORE
# ============================================================




# === TITAN V6.2 PROFESSIONAL MODULES ===
# 1) Platt + Isotonic calibration from paper trades
# 2) BTC correlation filter for alts in high-vol
# 3) Order-Block / Fair Value Gap (FVG)
# 4) Depth WebSocket for default liquid symbols only

DEPTH_WATCHLIST = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]
DEPTH_STATE: dict[str, dict[str, Any]] = {}
DEPTH_LOCK = threading.RLock()
BTC_RETURNS_CACHE: dict[str, Any] = {"ts": 0.0, "rets": None}

# Short-TTL cache for per-symbol BTC correlation (avoids repeated kline fetches)
BTC_CORR_CACHE: dict[str, tuple[float, dict]] = {}
BTC_CORR_CACHE_TTL = 90.0




def _pava_isotonic(xs: list[float], ys: list[float], ws: Optional[list[float]] = None) -> list[float]:
    """Pool-Adjacent-Violators for non-decreasing calibration curve."""
    n = len(ys)
    if n == 0:
        return []
    w = ws if ws and len(ws) == n else [1.0] * n
    y = list(ys)
    weight = [float(x) for x in w]
    i = 0
    while i < n - 1:
        if y[i] <= y[i + 1] + 1e-12:
            i += 1
            continue
        # merge i and i+1
        total_w = weight[i] + weight[i + 1]
        avg = (y[i] * weight[i] + y[i + 1] * weight[i + 1]) / max(total_w, 1e-12)
        y[i] = avg
        weight[i] = total_w
        del y[i + 1]
        del weight[i + 1]
        n -= 1
        if i > 0:
            i -= 1
    # expand back — simple: rebuild from unique pools by re-running with block sizes
    # For sparse bins we already merged; return monotone ys of same length as input via projection
    # Rebuild properly:
    y0 = list(ys)
    w0 = [float(x) for x in (ws if ws and len(ws) == len(ys) else [1.0] * len(ys))]
    blocks = [[i] for i in range(len(y0))]
    vals = [y0[i] for i in range(len(y0))]
    weights = [w0[i] for i in range(len(y0))]
    changed = True
    while changed:
        changed = False
        i = 0
        while i < len(vals) - 1:
            if vals[i] > vals[i + 1] + 1e-12:
                new_w = weights[i] + weights[i + 1]
                new_v = (vals[i] * weights[i] + vals[i + 1] * weights[i + 1]) / max(new_w, 1e-12)
                vals[i] = new_v
                weights[i] = new_w
                blocks[i] = blocks[i] + blocks[i + 1]
                del vals[i + 1]
                del weights[i + 1]
                del blocks[i + 1]
                changed = True
                if i > 0:
                    i -= 1
            else:
                i += 1
    out = [0.0] * len(y0)
    for b, v in zip(blocks, vals):
        for idx in b:
            out[idx] = v
    return out


def _platt_scale(raw_probs: list[float], labels: list[float], x_query: float) -> Optional[float]:
    """Fit simple Platt logistic P = 1/(1+exp(A*f+B)) via Newton on 2 params; return calibrated for x_query in [0,1]."""
    try:
        import math as _m
        n = len(raw_probs)
        if n < 12:
            return None
        # features as logit of raw
        f = []
        for p in raw_probs:
            p = clamp(p, 1e-4, 1 - 1e-4)
            f.append(_m.log(p / (1 - p)))
        y = labels
        A, B = 0.0, 0.0
        for _ in range(40):
            gA = gB = 0.0
            hAA = hAB = hBB = 0.0
            for fi, yi in zip(f, y):
                z = A * fi + B
                # stable sigmoid
                if z >= 0:
                    ez = _m.exp(-z)
                    p = 1.0 / (1.0 + ez)
                else:
                    ez = _m.exp(z)
                    p = ez / (1.0 + ez)
                diff = p - yi
                gA += diff * fi
                gB += diff
                w = p * (1 - p)
                hAA += w * fi * fi
                hAB += w * fi
                hBB += w
            # damped Newton
            det = hAA * hBB - hAB * hAB
            if abs(det) < 1e-12:
                break
            dA = (hBB * gA - hAB * gB) / det
            dB = (hAA * gB - hAB * gA) / det
            A -= 0.7 * dA
            B -= 0.7 * dB
            if abs(dA) + abs(dB) < 1e-6:
                break
        xq = clamp(x_query, 1e-4, 1 - 1e-4)
        fq = _m.log(xq / (1 - xq))
        z = A * fq + B
        if z >= 0:
            ez = _m.exp(-z)
            return 1.0 / (1.0 + ez)
        ez = _m.exp(z)
        return ez / (1.0 + ez)
    except Exception:
        return None


def calibrate_success_probability(
    symbol: str = "",
    direction: str = "",
    raw_prob: float = 50.0,
) -> dict[str, Any]:
    """Platt + Isotonic calibration using forecasts AND closed paper trades.

    Returns calibrated probability in 0-100 scale with diagnostics.
    """
    raw = float(clamp(raw_prob, 1.0, 99.0))
    pairs: list[tuple[float, float]] = []
    sources_used: list[str] = []
    try:
        where = ["outcome IN ('WIN','LOSS')", "success_prob > 0"]
        args: list[Any] = []
        if symbol:
            where.append("symbol=?")
            args.append(symbol)
        if direction in {"صعودی", "نزولی", "LONG", "SHORT"}:
            # map
            dmap = {"LONG": "صعودی", "SHORT": "نزولی"}
            dval = dmap.get(direction, direction)
            where.append("direction=?")
            args.append(dval)
        with DB_LOCK, db_conn() as con:
            rows = con.execute(
                "SELECT success_prob, outcome FROM forecasts WHERE " + " AND ".join(where)
                + " ORDER BY evaluated_at DESC, id DESC LIMIT 800",
                args,
            ).fetchall()
            for r in rows:
                pp = safe_float(r[0], 50) / 100.0
                yy = 1.0 if str(r[1]).upper() == "WIN" else 0.0
                if math.isfinite(pp):
                    pairs.append((clamp(pp, 0, 1), yy))
            if pairs:
                sources_used.append("forecasts")
            # Paper trades with stored entry probability contribute honest samples.
            paper_where = ["status='CLOSED'", "success_prob > 0", "pnl_pct IS NOT NULL"]
            paper_args: list[Any] = []
            if symbol:
                paper_where.append("symbol=?")
                paper_args.append(symbol)
            if direction in {"صعودی", "نزولی", "LONG", "SHORT"}:
                dmap = {"LONG": "صعودی", "SHORT": "نزولی"}
                dval = dmap.get(direction, direction)
                paper_where.append("decision=?")
                paper_args.append(dval)
            prows = con.execute(
                "SELECT success_prob, pnl_pct FROM paper_trades WHERE "
                + " AND ".join(paper_where)
                + " ORDER BY closed_at DESC, id DESC LIMIT 400",
                paper_args,
            ).fetchall()
            paper_n = 0
            for r in prows:
                pp = safe_float(r[0], 0) / 100.0
                pnl = safe_float(r[1], 0)
                if not math.isfinite(pp) or pp <= 0:
                    continue
                yy = 1.0 if pnl > 0 else 0.0
                pairs.append((clamp(pp, 0, 1), yy))
                paper_n += 1
            if paper_n:
                sources_used.append("paper_trades")
    except Exception as exc:
        LOGGER.debug("calibration data load failed: %s", exc)

    if len(pairs) < 8:
        return {
            "raw": round(raw, 1),
            "calibrated": round(raw, 1),
            "samples": len(pairs),
            "method": "prior_only",
            "platt": None,
            "isotonic": None,
            "sources": sources_used,
        }

    probs = [p for p, _ in pairs]
    labels = [y for _, y in pairs]
    x = raw / 100.0

    # Platt
    platt_p = _platt_scale(probs, labels, x)

    # Isotonic on 10 bins
    bins = []
    for k in range(10):
        lo, hi = k / 10, (k + 1) / 10
        vals = [y for px, y in pairs if (lo <= px < hi) or (k == 9 and lo <= px <= hi)]
        if vals:
            rate = (sum(vals) + 2.0) / (len(vals) + 4.0)  # Beta(2,2)
            bins.append((lo + 0.05, rate, len(vals)))
    xs = [b[0] for b in bins]
    ys = [b[1] for b in bins]
    ws = [b[2] for b in bins]
    iso = _pava_isotonic(xs, ys, ws) if bins else []
    iso_p = None
    if bins and iso:
        if x <= xs[0]:
            iso_p = iso[0]
        elif x >= xs[-1]:
            iso_p = iso[-1]
        else:
            j = 0
            for i in range(len(xs) - 1):
                if xs[i] <= x <= xs[i + 1]:
                    j = i
                    break
            span = max(xs[j + 1] - xs[j], 1e-9)
            iso_p = iso[j] + (iso[j + 1] - iso[j]) * (x - xs[j]) / span

    # Blend: prefer isotonic when samples high; Platt for smooth mid-range
    n = len(pairs)
    if platt_p is not None and iso_p is not None:
        w_iso = min(0.65, n / 120.0)
        cal = (1 - w_iso) * platt_p + w_iso * iso_p
        method = "platt+isotonic"
    elif iso_p is not None:
        cal = iso_p
        method = "isotonic"
    elif platt_p is not None:
        cal = platt_p
        method = "platt"
    else:
        cal = x
        method = "raw"

    # Shrink toward raw when n small
    shrink = min(1.0, n / 80.0)
    cal = shrink * cal + (1 - shrink) * x
    # Conservative cap: never claim >88% from calibration alone
    cal = clamp(cal, 0.08, 0.88)

    # ECE / Brier rough
    brier = sum((p - y) ** 2 for p, y in pairs) / max(1, n)
    ece = 0.0
    if bins:
        for (_, rate, cnt), mid in zip(bins, xs):
            ece += (cnt / n) * abs(rate - mid)

    return {
        "raw": round(raw, 1),
        "calibrated": round(cal * 100.0, 1),
        "samples": n,
        "method": method,
        "platt": round(platt_p * 100, 1) if platt_p is not None else None,
        "isotonic": round(iso_p * 100, 1) if iso_p is not None else None,
        "ece": round(ece, 4),
        "brier": round(brier, 4),
        "sources": sources_used,
    }


def compute_btc_correlation(symbol: str, lookback: int = 48) -> dict[str, Any]:
    """Pearson correlation of recent 1h returns vs BTC — for high-vol alt filter."""
    try:
        if symbol in {"BTC/USDT", "BTCUSDT"}:
            return {"corr": 1.0, "samples": lookback, "regime": "self"}
        now = time.time()
        _ck = f"{_normalize_symbol(symbol)}|{lookback}"
        _hit = BTC_CORR_CACHE.get(_ck)
        if _hit and (now - _hit[0]) < BTC_CORR_CACHE_TTL:
            return _hit[1]
        global BTC_RETURNS_CACHE
        btc_rets = BTC_RETURNS_CACHE.get("rets")
        if btc_rets is None or now - float(BTC_RETURNS_CACHE.get("ts") or 0) > 180:
            btc_df = fetch_klines("BTC/USDT", "1h", lookback + 5)
            btc_rets = btc_df["close"].astype(float).pct_change().dropna().tail(lookback)
            BTC_RETURNS_CACHE = {"ts": now, "rets": btc_rets}
        alt_df = fetch_klines(symbol, "1h", lookback + 5)
        alt_rets = alt_df["close"].astype(float).pct_change().dropna().tail(lookback)
        n = min(len(btc_rets), len(alt_rets))
        if n < 12:
            return {"corr": 0.0, "samples": n, "regime": "insufficient"}
        a = alt_rets.tail(n).values
        b = btc_rets.tail(n).values
        if float(np.std(a)) < 1e-12 or float(np.std(b)) < 1e-12:
            return {"corr": 0.0, "samples": n, "regime": "flat"}
        corr = float(np.corrcoef(a, b)[0, 1])
        if not math.isfinite(corr):
            corr = 0.0
        _out = {"corr": round(corr, 3), "samples": n, "regime": "high_beta" if corr >= 0.75 else "decoupled" if corr < 0.35 else "normal"}
        BTC_CORR_CACHE[_ck] = (now, _out)
        if len(BTC_CORR_CACHE) > 60:
            for _k, _ in sorted(BTC_CORR_CACHE.items(), key=lambda x: x[1][0])[:15]:
                BTC_CORR_CACHE.pop(_k, None)
        return _out
    except Exception as exc:
        LOGGER.debug("btc corr failed %s: %s", symbol, exc)
        return {"corr": 0.0, "samples": 0, "regime": "error"}


def apply_btc_correlation_filter(
    symbol: str,
    decision_tag: str,
    bias: str,
    atr_pct: float,
    btc_trend: str,
    corr_info: Optional[dict] = None,
) -> tuple[str, str, dict[str, Any]]:
    """In high-vol regimes, block alt longs when BTC is bearish & correlation is high (and vice versa)."""
    info = corr_info or compute_btc_correlation(symbol)
    note = {
        "corr": info.get("corr"),
        "corr_regime": info.get("regime"),
        "filter_applied": False,
        "reason": "",
    }
    if symbol in {"BTC/USDT", "BTCUSDT"}:
        return decision_tag, bias, note
    corr = safe_float(info.get("corr"), 0)
    high_vol = atr_pct >= 3.5
    # Only act in high volatility or very high beta
    if not high_vol and corr < 0.8:
        return decision_tag, bias, note
    if corr < 0.55:
        note["reason"] = "همبستگی پایین با BTC — فیلتر غیرفعال"
        return decision_tag, bias, note

    if decision_tag == "LONG" and btc_trend == "نزولی" and corr >= 0.65:
        note["filter_applied"] = True
        note["reason"] = f"آلت لانگ در رژیم پرنوسان با همبستگی BTC={corr:.2f} و روند نزولی BTC مسدود شد"
        return "WAIT", "خنثی", note
    if decision_tag == "SHORT" and btc_trend == "صعودی" and corr >= 0.65:
        note["filter_applied"] = True
        note["reason"] = f"آلت شورت در رژیم پرنوسان با همبستگی BTC={corr:.2f} و روند صعودی BTC مسدود شد"
        return "WAIT", "خنثی", note
    note["reason"] = "فیلتر همبستگی BTC عبور کرد"
    return decision_tag, bias, note


def detect_order_blocks_fvg(df: pd.DataFrame, lookback: int = 40) -> dict[str, Any]:
    """Simple Order-Block and Fair Value Gap detection on closed candles.

    Bullish OB: last down candle before strong impulsive up move.
    Bearish OB: last up candle before strong impulsive down move.
    FVG: 3-candle gap where candle[i-2].high < candle[i].low (bull) or reverse (bear).
    """
    try:
        if df is None or len(df) < 15:
            return {"order_blocks": [], "fvgs": [], "nearest_ob": None, "nearest_fvg": None}
        d = df.tail(lookback).reset_index(drop=True)
        o = d["open"].astype(float)
        h = d["high"].astype(float)
        l = d["low"].astype(float)
        c = d["close"].astype(float)
        atr_s = (h - l).rolling(14).mean().bfill()
        obs: list[dict[str, Any]] = []
        fvgs: list[dict[str, Any]] = []
        price = float(c.iloc[-1])

        for i in range(3, len(d) - 1):
            body = abs(float(c.iloc[i]) - float(o.iloc[i]))
            atr_i = float(atr_s.iloc[i]) or price * 0.01
            move = float(c.iloc[i]) - float(c.iloc[i - 1])
            # Bullish FVG: gap up between candle i-2 high and candle i low
            if float(h.iloc[i - 2]) < float(l.iloc[i]):
                gap_lo, gap_hi = float(h.iloc[i - 2]), float(l.iloc[i])
                if gap_hi - gap_lo > 0.15 * atr_i:
                    fvgs.append({
                        "type": "bullish_fvg",
                        "low": round(gap_lo, 8),
                        "high": round(gap_hi, 8),
                        "mid": round((gap_lo + gap_hi) / 2, 8),
                        "index": i,
                        "guide": "گپ صعودی پرنشده — اغلب به‌عنوان حمایت پویا عمل می‌کند؛ پر شدن گپ می‌تواند ادامه یا برگشت باشد.",
                    })
            # Bearish FVG
            if float(l.iloc[i - 2]) > float(h.iloc[i]):
                gap_hi, gap_lo = float(l.iloc[i - 2]), float(h.iloc[i])
                if gap_hi - gap_lo > 0.15 * atr_i:
                    fvgs.append({
                        "type": "bearish_fvg",
                        "low": round(gap_lo, 8),
                        "high": round(gap_hi, 8),
                        "mid": round((gap_lo + gap_hi) / 2, 8),
                        "index": i,
                        "guide": "گپ نزولی پرنشده — اغلب به‌عنوان مقاومت پویا؛ پر شدن گپ نشانه فشار خرید یا ادامه نزول است.",
                    })

            # Order block: impulse after opposite candle
            if move > 1.2 * atr_i and float(c.iloc[i - 1]) < float(o.iloc[i - 1]):
                # bullish OB = prior bearish candle
                obs.append({
                    "type": "bullish_ob",
                    "low": round(float(l.iloc[i - 1]), 8),
                    "high": round(float(h.iloc[i - 1]), 8),
                    "mid": round((float(l.iloc[i - 1]) + float(h.iloc[i - 1])) / 2, 8),
                    "index": i - 1,
                    "guide": "بلاک سفارش صعودی: آخرین کندل نزولی قبل از حرکت قوی بالا. بازگشت قیمت به این ناحیه می‌تواند تقاضا را فعال کند.",
                })
            if move < -1.2 * atr_i and float(c.iloc[i - 1]) > float(o.iloc[i - 1]):
                obs.append({
                    "type": "bearish_ob",
                    "low": round(float(l.iloc[i - 1]), 8),
                    "high": round(float(h.iloc[i - 1]), 8),
                    "mid": round((float(l.iloc[i - 1]) + float(h.iloc[i - 1])) / 2, 8),
                    "index": i - 1,
                    "guide": "بلاک سفارش نزولی: آخرین کندل صعودی قبل از حرکت قوی پایین. بازگشت به این ناحیه می‌تواند عرضه را فعال کند.",
                })

        # Keep nearest to price
        def _dist(zone):
            return abs(price - safe_float(zone.get("mid"), price))

        obs = sorted(obs, key=_dist)[:4]
        fvgs = sorted(fvgs, key=_dist)[:4]
        nearest_ob = obs[0] if obs else None
        nearest_fvg = fvgs[0] if fvgs else None
        return {
            "order_blocks": obs,
            "fvgs": fvgs,
            "nearest_ob": nearest_ob,
            "nearest_fvg": nearest_fvg,
            "price": round(price, 8),
        }
    except Exception as exc:
        LOGGER.debug("OB/FVG failed: %s", exc)
        return {"order_blocks": [], "fvgs": [], "nearest_ob": None, "nearest_fvg": None}


def get_depth_snapshot(symbol: str) -> dict[str, Any]:
    """Read cached partial book depth for watchlist symbols."""
    pair = _binance_symbol(symbol)
    with DEPTH_LOCK:
        row = DEPTH_STATE.get(pair) or DEPTH_STATE.get(symbol) or {}
    if not row:
        return {"available": False, "symbol": pair}
    age = time.time() - safe_float(row.get("ts"), 0)
    return {
        "available": age < 30,
        "symbol": pair,
        "bid": row.get("bid"),
        "ask": row.get("ask"),
        "spread_bps": row.get("spread_bps"),
        "bid_vol": row.get("bid_vol"),
        "ask_vol": row.get("ask_vol"),
        "imbalance": row.get("imbalance"),
        "age_sec": round(age, 1),
        "source": row.get("source", "depth"),
    }


def _depth_websocket_worker() -> None:
    """Lightweight partial book depth for a few liquid symbols only (mobile-friendly)."""
    if _websocket_client is None:
        return
    streams = "/".join(f"{s.lower()}@depth5@1000ms" for s in DEPTH_WATCHLIST)
    url = f"wss://data-stream.binance.vision/stream?streams={streams}"
    while not LIVE_STOP.is_set():
        try:
            ws = _websocket_client.create_connection(url, timeout=15)
            ws.settimeout(25)
            LOGGER.info("Depth WebSocket connected for %s", DEPTH_WATCHLIST)
            while not LIVE_STOP.is_set():
                raw = ws.recv()
                if not raw:
                    break
                try:
                    msg = json.loads(raw)
                    data = msg.get("data") or msg
                    bids = data.get("bids") or data.get("b") or []
                    asks = data.get("asks") or data.get("a") or []
                    stream = str(msg.get("stream") or "")
                    sym = stream.split("@")[0].upper() if "@" in stream else ""
                    if not sym and data.get("s"):
                        sym = str(data["s"]).upper()
                    if not bids or not asks or not sym:
                        continue
                    best_bid = safe_float(bids[0][0])
                    best_ask = safe_float(asks[0][0])
                    bid_vol = sum(safe_float(x[1]) for x in bids[:5])
                    ask_vol = sum(safe_float(x[1]) for x in asks[:5])
                    mid = (best_bid + best_ask) / 2 if best_bid and best_ask else 0
                    spread_bps = ((best_ask - best_bid) / mid * 10000) if mid else 0
                    imb = (bid_vol - ask_vol) / max(bid_vol + ask_vol, 1e-12)
                    with DEPTH_LOCK:
                        DEPTH_STATE[sym] = {
                            "bid": best_bid,
                            "ask": best_ask,
                            "bid_vol": round(bid_vol, 4),
                            "ask_vol": round(ask_vol, 4),
                            "spread_bps": round(spread_bps, 2),
                            "imbalance": round(imb, 4),
                            "ts": time.time(),
                            "source": "DepthWS",
                        }
                except Exception:
                    continue
            try:
                ws.close()
            except Exception:
                _swallow()
        except Exception as exc:
            LOGGER.info("Depth WS unavailable (REST only): %s", exc)
            LIVE_STOP.wait(8)



def wilder_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).replace([np.inf, -np.inf], np.nan).fillna(50.0)


def calc_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    previous_close = df["close"].shift(1)
    tr = pd.concat([(df["high"] - df["low"]), (df["high"] - previous_close).abs(), (df["low"] - previous_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean().bfill()


def calc_vwap(df: pd.DataFrame) -> float:
    if df.empty:
        return float("nan")
    timestamps = pd.to_datetime(df["t"], unit="ms", utc=True)
    latest_day = timestamps.iloc[-1].date()
    session_df = df[timestamps.dt.date == latest_day]
    if session_df.empty:
        session_df = df
    typical = (session_df["high"] + session_df["low"] + session_df["close"]) / 3.0
    volume_sum = float(session_df["vol"].sum())
    return float((typical * session_df["vol"]).sum() / volume_sum) if volume_sum > 0 else float(session_df["close"].iloc[-1])


def _tf_forecast(df: pd.DataFrame, fast: int = 20, slow: int = 50, tf: str | None = None) -> tuple[str, float, dict[str, float]]:
    """Multi-factor TF score: EMA structure + momentum + RSI + ADX + volume + MACD + candle pressure.

    Symmetric for LONG/SHORT. Higher ADX amplifies directional conviction; low ADX shrinks
    extremes toward neutral so sideways markets do not fake strong signals.
    """
    if df is None or len(df) < slow + 5:
        return "خنثی", 50.0, {"rsi": 50.0, "momentum": 0.0, "ema_gap": 0.0, "adx": 0.0, "vol_z": 0.0}
    profile = v59_tf_policy(tf) if tf in V59_TF_POLICY else {"fast": fast, "slow": slow, "momentum_bars": 8, "long": 54.0, "short": 46.0}
    fast = int(profile.get("fast", fast)); slow = int(profile.get("slow", slow))
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    vol = df["vol"].astype(float) if "vol" in df.columns else pd.Series([1.0] * len(df))
    fast_ema = close.ewm(span=fast, adjust=False).mean()
    slow_ema = close.ewm(span=slow, adjust=False).mean()
    rsi_value = float(wilder_rsi(close).iloc[-1])
    lookback = min(int(profile.get("momentum_bars", 8)), len(close) - 1)
    momentum = (float(close.iloc[-1]) / float(close.iloc[-1 - lookback]) - 1) * 100
    ema_gap = (float(fast_ema.iloc[-1]) / float(slow_ema.iloc[-1]) - 1) * 100

    # ADX (trend strength) — soft gate
    adx_val = 20.0
    try:
        if "_adx_series" in globals():
            adx_s = _adx_series(df, 14)
            adx_val = float(adx_s.iloc[-1]) if adx_s is not None and len(adx_s) else 20.0
        else:
            # lightweight ATR-based trend proxy
            tr = pd.concat([(high - low), (high - close.shift(1)).abs(), (low - close.shift(1)).abs()], axis=1).max(axis=1)
            atr14 = tr.ewm(alpha=1/14, adjust=False).mean()
            up = high.diff().clip(lower=0)
            dn = (-low.diff()).clip(lower=0)
            plus_dm = up.where(up > dn, 0.0)
            minus_dm = dn.where(dn > up, 0.0)
            plus_di = 100 * plus_dm.ewm(alpha=1/14, adjust=False).mean() / atr14.replace(0, np.nan)
            minus_di = 100 * minus_dm.ewm(alpha=1/14, adjust=False).mean() / atr14.replace(0, np.nan)
            dx = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)).fillna(20)
            adx_val = float(dx.ewm(alpha=1/14, adjust=False).mean().iloc[-1])
    except Exception:
        adx_val = 20.0
    adx_val = float(clamp(adx_val if math.isfinite(adx_val) else 20.0, 0, 60))

    # Volume z-score (last vs 20-bar mean)
    vol_z = 0.0
    try:
        v_mean = float(vol.tail(21).iloc[:-1].mean()) or 1.0
        v_std = float(vol.tail(21).iloc[:-1].std()) or 1.0
        vol_z = (float(vol.iloc[-1]) - v_mean) / max(v_std, 1e-9)
        vol_z = float(np.clip(vol_z, -3.0, 3.0))
    except Exception:
        vol_z = 0.0

    # MACD hist tilt
    macd_tilt = 0.0
    try:
        m = calc_macd(close)
        hist = safe_float(m.get("hist"), 0)
        last_px = float(close.iloc[-1]) or 1.0
        macd_tilt = float(np.clip(hist / max(last_px * 0.002, 1e-12), -1.5, 1.5))
        if m.get("cross") == "bull":
            macd_tilt += 0.35
        elif m.get("cross") == "bear":
            macd_tilt -= 0.35
    except Exception:
        _swallow()

    # Candle body pressure (last 3)
    pressure = 0.0
    try:
        for i in range(-3, 0):
            o, c = float(df["open"].iloc[i]), float(close.iloc[i])
            rng = max(float(high.iloc[i]) - float(low.iloc[i]), 1e-12)
            pressure += ((c - o) / rng) * (0.5 if i == -3 else 0.75 if i == -2 else 1.0)
        pressure = float(np.clip(pressure / 2.25, -1.0, 1.0))
    except Exception:
        pressure = 0.0

    score = 50.0
    # Structure (EMA) — primary directional spine
    score += 14.0 if float(close.iloc[-1]) > float(fast_ema.iloc[-1]) else -14.0
    score += 12.0 if float(fast_ema.iloc[-1]) > float(slow_ema.iloc[-1]) else -12.0
    # Momentum
    score += float(np.clip(momentum * 3.2, -12.0, 12.0))
    # RSI mean-reversion mild + trend confirmation
    if rsi_value < 28:
        score += 4.0
    elif rsi_value < 38:
        score += 1.5
    elif rsi_value > 72:
        score -= 4.0
    elif rsi_value > 62:
        score -= 1.5
    # EMA gap magnitude
    score += float(np.clip(ema_gap * 1.1, -5.5, 5.5))
    # MACD + candle pressure
    score += macd_tilt * 4.5
    score += pressure * 5.0
    # Volume confirms move direction
    if abs(momentum) > 0.15:
        score += float(np.clip(vol_z * (1.0 if momentum > 0 else -1.0) * 2.2, -5.0, 5.0))

    # ADX scaling: low ADX compresses toward 50; high ADX preserves extremes
    adx_scale = float(clamp(0.55 + (adx_val / 40.0) * 0.55, 0.55, 1.15))
    score = 50.0 + (score - 50.0) * adx_scale
    score = float(clamp(score, 0, 100))

    long_cut = float(profile.get("long", 54.0)); short_cut = float(profile.get("short", 46.0))
    direction = "صعودی" if score >= long_cut else "نزولی" if score <= short_cut else "خنثی"
    return direction, score, {
        "rsi": round(rsi_value, 2),
        "momentum": round(momentum, 4),
        "ema_gap": round(ema_gap, 4),
        "adx": round(adx_val, 2),
        "vol_z": round(vol_z, 3),
        "macd_tilt": round(macd_tilt, 3),
        "pressure": round(pressure, 3),
        "adx_scale": round(adx_scale, 3),
    }


def _funding_bias(funding: Optional[float]) -> tuple[float, str]:
    if funding is None:
        return 0.0, "فاقد داده"
    if funding >= 0.08:
        return -7.0, "ازدحام لانگ / ریسک بازگشت"
    if funding >= 0.03:
        return -3.0, "فشار لانگ متوسط"
    if funding <= -0.08:
        return 7.0, "ازدحام شورت / ریسک پوشش شورت"
    if funding <= -0.03:
        return 3.0, "فشار شورت متوسط"
    return 0.0, "خنثی"


def _oi_bias(oi_delta: float, price_change: float) -> tuple[float, str]:
    if not math.isfinite(oi_delta):
        return 0.0, "فاقد داده"
    if price_change > 0 and oi_delta > 1.0:
        return 4.0, "افزایش قیمت + افزایش OI"
    if price_change < 0 and oi_delta > 1.0:
        return -4.0, "کاهش قیمت + افزایش OI"
    if price_change > 0 and oi_delta < -1.0:
        return 2.0, "افزایش قیمت + کاهش OI"
    if price_change < 0 and oi_delta < -1.0:
        return -2.0, "کاهش قیمت + کاهش OI"
    return 0.0, "OI بدون تأیید قوی"



def _adx_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder-style ADX without external dependencies."""
    if df is None or df.empty or len(df) < period + 3:
        return pd.Series(dtype=float)
    high, low, close = df["high"].astype(float), df["low"].astype(float), df["close"].astype(float)
    up = high.diff(); down = -low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    tr = pd.concat([(high-low), (high-close.shift()).abs(), (low-close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    pdi = 100 * plus_dm.ewm(alpha=1/period, adjust=False, min_periods=period).mean() / atr.replace(0, np.nan)
    mdi = 100 * minus_dm.ewm(alpha=1/period, adjust=False, min_periods=period).mean() / atr.replace(0, np.nan)
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1/period, adjust=False, min_periods=period).mean().fillna(0.0)
