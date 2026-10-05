        # neutral core still allows promotion from structure/regime
        if score >= 55:
            long_votes += 0.5
        elif score <= 45:
            short_votes += 0.5

    if struct_bias == "صعودی":
        long_votes += 1.5
    elif struct_bias == "نزولی":
        short_votes += 1.5

    if regime_name in {"trend_up", "breakout_watch"} or "up" in regime_name:
        long_votes += 1.2
    elif regime_name in {"trend_down"} or "down" in regime_name:
        short_votes += 1.2
    elif regime_name == "high_volatility":
        # volatility does not favor either side
        pass

    # Confluence strengthens whichever side score already leans
    if conf_score >= 68:
        if score >= 55:
            long_votes += 1.0
        elif score <= 45:
            short_votes += 1.0
    if conf_score >= 78:
        if score >= 52:
            long_votes += 0.5
        elif score <= 48:
            short_votes += 0.5

    if meta_label == "ACCEPT" and meta_prob >= 68:
        if score >= 54:
            long_votes += 1.0
        elif score <= 46:
            short_votes += 1.0

    # V58: balanced promotion — fewer false WAITs when layers agree; still reject noise.
    # High-conviction directional
    if long_votes >= 2.6 and short_votes <= 1.9 and alignment >= 46 and score >= 50:
        return "صعودی", "LONG"
    if short_votes >= 2.6 and long_votes <= 1.9 and alignment >= 46 and score <= 50:
        return "نزولی", "SHORT"

    # Moderate conviction (symmetric)
    if bias == "صعودی" and long_votes >= short_votes + 0.45 and score >= 52 and alignment >= 46:
        return "صعودی", "LONG"
    if bias == "نزولی" and short_votes >= long_votes + 0.45 and score <= 48 and alignment >= 46:
        return "نزولی", "SHORT"

    # Score-led when layers agree mildly
    if long_votes >= short_votes + 0.9 and score >= 54 and alignment >= 48:
        return "صعودی", "LONG"
    if short_votes >= long_votes + 0.9 and score <= 46 and alignment >= 48:
        return "نزولی", "SHORT"

    # Structure-led override when quant is neutral but structure is clear
    if bias == "خنثی":
        if long_votes >= 2.4 and short_votes <= 1.3 and score >= 51 and alignment >= 50:
            return "صعودی", "LONG"
        if short_votes >= 2.4 and long_votes <= 1.3 and score <= 49 and alignment >= 50:
            return "نزولی", "SHORT"

    return "خنثی", "WAIT"

# ============================================================
# AI LAYER
# ============================================================

DEFAULT_MODEL_SETTINGS = {
    "openai_models": ["gpt-4o-mini"],
    "gemini_models": ["gemini-2.5-flash", "gemini-2.0-flash"],
    "grok_models": ["grok-4-1-fast-reasoning"],
    "claude_models": ["claude-sonnet-4-6"],
    "deepseek_models": ["deepseek-chat"],
}


def _load_model_settings() -> dict[str, Any]:
    data = _load_json(MODEL_SETTINGS_PATH, DEFAULT_MODEL_SETTINGS)
    out = {k: list(v) for k, v in DEFAULT_MODEL_SETTINGS.items()}
    if isinstance(data, dict):
        for key in out:
            if isinstance(data.get(key), list):
                vals = [str(x).strip() for x in data[key] if str(x).strip()]
                if vals: out[key] = vals
    return out


def _build_ai_prompt(payload: dict[str, Any]) -> str:
    return (
        "دستور اجباری زبان: فقط فارسی. هیچ کلمه لاتین، انگلیسی یا مخفف انگلیسی ننویس. "
        "به‌جای Long بگو خرید/صعودی، به‌جای Short بگو فروش/نزولی، به‌جای Wait بگو انتظار. "
        "به‌جای RSI بگو شاخص قدرت نسبی، به‌جای VWAP بگو میانگین وزنی حجم، به‌جای Funding بگو نرخ تأمین مالی. "
        "تو تحلیل‌گر محافظه‌کار بازار رمزارز هستی. فقط از داده‌های زیر استفاده کن. تضمین سود نده. "
        "تناقض روند، مومنتوم، میانگین‌ها، حجم و مشتقات را بگو. حداکثر ۵ جمله کوتاه فارسی روان. "
        "جمله آخر دقیقاً با یکی از این سه قالب تمام شود: «نتیجه نهایی: صعودی» یا «نتیجه نهایی: نزولی» یا «نتیجه نهایی: انتظار».\n"
        + json.dumps(payload, ensure_ascii=False, default=str)
    )


def _cache_key(provider: str, model: str, payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps({"provider": provider, "model": model, "payload": payload}, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _cached_ai(provider: str, model: str, payload: dict[str, Any], ttl: int = AI_CACHE_TTL) -> Optional[str]:
    cache = _load_json(AI_CACHE_PATH, {})
    item = cache.get(_cache_key(provider, model, payload)) if isinstance(cache, dict) else None
    if isinstance(item, dict) and time.time() - safe_float(item.get("ts"), 0) < ttl:
        return str(item.get("text")) if item.get("text") else None
    return None


def _store_ai(provider: str, model: str, payload: dict[str, Any], text: str) -> None:
    cache = _load_json(AI_CACHE_PATH, {})
    if not isinstance(cache, dict): cache = {}
    cache[_cache_key(provider, model, payload)] = {"ts": time.time(), "text": text}
    if len(cache) > 800:
        items = sorted(cache.items(), key=lambda x: safe_float(x[1].get("ts"), 0))
        for key, _ in items[:len(cache) - 800]: cache.pop(key, None)
    _save_json(AI_CACHE_PATH, cache)


def _persianize_ai_text(value: str) -> str:
    """Light glossary swap so dashboard stays Persian even if model leaks English terms."""
    if not value:
        return value
    pairs = [
        ("LONG", "خرید"), ("SHORT", "فروش"), ("WAIT", "انتظار"),
        ("Long", "خرید"), ("Short", "فروش"), ("Wait", "انتظار"),
        ("bullish", "صعودی"), ("bearish", "نزولی"), ("neutral", "خنثی"),
        ("Bullish", "صعودی"), ("Bearish", "نزولی"), ("Neutral", "خنثی"),
        ("support", "حمایت"), ("resistance", "مقاومت"),
        ("Support", "حمایت"), ("Resistance", "مقاومت"),
        ("Funding", "نرخ تأمین مالی"), ("funding", "نرخ تأمین مالی"),
        ("Open Interest", "بهره باز"), ("open interest", "بهره باز"),
        ("momentum", "مومنتوم"), ("Momentum", "مومنتوم"),
        ("breakout", "شکست سطح"), ("Breakout", "شکست سطح"),
        ("overbought", "اشباع خرید"), ("oversold", "اشباع فروش"),
        ("risk", "ریسک"), ("Risk", "ریسک"),
        ("entry", "ورود"), ("stop loss", "حد ضرر"), ("take profit", "حد سود"),
        ("Buy", "خرید"), ("Sell", "فروش"), ("buy", "خرید"), ("sell", "فروش"),
        ("HIGH CONVICTION", "اطمینان بالا"), ("CONFIRMED SETUP", "ستاپ تأییدشده"),
        ("WATCH", "تحت نظر"), ("LOW EDGE", "لبه ضعیف"),
    ]
    out = value
    for en, fa in pairs:
        out = out.replace(en, fa)
    return out


def _clean_ai_text(text: Any) -> str:
    """Normalize AI output and push toward Persian dashboard display."""
    if text is None:
        return ""
    if isinstance(text, (list, tuple)):
        text = " ".join(str(x) for x in text)
    value = " ".join(str(text).strip().split())
    if not value:
        return ""
    value = _persianize_ai_text(value)
    return value[:12000]


def _extract_gemini_text(response: dict[str, Any]) -> str:
    chunks: list[str] = []
    for candidate in response.get("candidates", []) or []:
        for part in (candidate.get("content") or {}).get("parts", []) or []:
            if part.get("text"): chunks.append(str(part["text"]))
    return _clean_ai_text(" ".join(chunks))


def _extract_chat_completion(response: dict[str, Any]) -> str:
    try:
        choices = response.get("choices") or []
        if not choices: return ""
        content = (choices[0].get("message") or {}).get("content", "")
        if isinstance(content, list):
            content = " ".join(str(x.get("text", "")) for x in content if isinstance(x, dict))
        return _clean_ai_text(content)
    except Exception:
        return ""


def _extract_openai_response(response: dict[str, Any]) -> str:
    """Extract text from the OpenAI Responses API across compatible response shapes."""
    try:
        # Preferred Responses API convenience field.
        output_text = response.get("output_text")
        if output_text:
            return _clean_ai_text(output_text)

        chunks: list[str] = []
        for item in response.get("output", []) or []:
            if not isinstance(item, dict):
                continue
            for content in item.get("content", []) or []:
                if not isinstance(content, dict):
                    continue
                if content.get("text"):
                    chunks.append(str(content["text"]))
        return _clean_ai_text(" ".join(chunks))
    except Exception:
        return ""


GEMINI_LAST_ERROR = ""
_GEMINI_MODEL_CACHE: dict[str, Any] = {"ts": 0.0, "models": []}
_GEMINI_MODEL_CACHE_TTL = 900.0
_GEMINI_MODEL_LOCK = threading.RLock()

def _gemini_available_models(force: bool = False) -> list[str]:
    """Return configured Gemini models first and cache API discovery.

    Model discovery is metadata, not market data; repeating it for every coin
    creates needless latency and can itself consume rate-limit budget.
    """
    configured = [str(x).strip() for x in _load_model_settings().get("gemini_models", []) if str(x).strip()]
    now = time.time()
    with _GEMINI_MODEL_LOCK:
        cached = list(_GEMINI_MODEL_CACHE.get("models") or [])
        if cached and not force and now - safe_float(_GEMINI_MODEL_CACHE.get("ts"), 0) < _GEMINI_MODEL_CACHE_TTL:
            return list(dict.fromkeys(configured + cached))

    discovered: list[str] = []
    if GEMINI_API_KEY:
        try:
            response = _request_json(
                "GET",
                "https://generativelanguage.googleapis.com/v1beta/models",
                headers={"x-goog-api-key": GEMINI_API_KEY},
                params={"pageSize": 100},
                timeout=12,
            )
            for item in (response or {}).get("models", []) or []:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", "")).strip()
                methods = item.get("supportedGenerationMethods", []) or []
                if name.startswith("models/"):
                    name = name.split("/", 1)[1]
                if name and "generateContent" in methods:
                    discovered.append(name)
        except Exception as exc:
            LOGGER.warning("Gemini model discovery failed: %s", exc)

    with _GEMINI_MODEL_LOCK:
        _GEMINI_MODEL_CACHE.update(ts=now, models=discovered)
    return list(dict.fromkeys(configured + discovered))

def _call_gemini(payload: dict[str, Any], global_summary: bool = False) -> Optional[str]:
    global GEMINI_LAST_ERROR
    GEMINI_LAST_ERROR = ""
    if not GEMINI_API_KEY:
        GEMINI_LAST_ERROR = "Gemini API key not found"
        return None
    prompt = (
        "تو یک تحلیلگر حرفه‌ای بازار رمزارز زیر نظر موتور TITAN ENTERPRISE هستی. "
        "بر اساس داده‌های واقعی زیر، تحلیل دقیق و کوتاه فارسی ارائه کن. "
        "فقط فارسی بنویس. هیچ واژه انگلیسی یا لاتین مجاز نیست. از ادعاهای بدون داده خودداری کن. خروجی مخصوص داشبورد فارسی باشد.\n"
        + ("یک جمع‌بندی کلان بازار در حداکثر 6 جمله ارائه کن.\n" if global_summary else _build_ai_prompt(payload))
        + json.dumps(payload, ensure_ascii=False, default=str)
    ) if global_summary else (
        "تو تحلیلگر اختصاصی TITAN ENTERPRISE هستی. پاسخ را فقط به زبان فارسی و مبتنی بر داده‌های زیر بده. ممنوعیت مطلق انگلیسی: اگر پاسخ داخلی انگلیسی بود همان را کامل به فارسی روان بازنویسی کن و فقط فارسی برگردان. "
        "جمع‌بندی باید برای داشبورد قابل نمایش باشد و شامل وضعیت بازار، دلیل اصلی، روند، مومنتوم، حجم، حمایت و مقاومت احتمالی، سطوح فیبوناچی، ریسک مهم و وضعیت تایم‌فریم‌ها باشد. خروجی فقط فارسی باشد.\n"
        + _build_ai_prompt(payload)
    )
    models = _gemini_available_models()
    if not models:
        GEMINI_LAST_ERROR = "No Gemini model supporting generateContent was found"
        return None
    for model in models:
        cached = _cached_ai("gemini", model, payload, 600 if global_summary else AI_CACHE_TTL)
        if cached:
            return cached
        response = _request_json(
            "POST",
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": 420, "temperature": 0.2},
            },
            timeout=35,
        )
        if response:
            text = _extract_gemini_text(response)
            if text:
                _store_ai("gemini", model, payload, text)
                return text
            feedback = response.get("promptFeedback") or {}
            block = feedback.get("blockReason") or ""
            finish = ""
            try:
                finish = (response.get("candidates") or [{}])[0].get("finishReason", "")
            except Exception:
                _swallow()
            GEMINI_LAST_ERROR = f"Gemini returned no text (blockReason={block or 'none'}, finishReason={finish or 'none'})"
        else:
            GEMINI_LAST_ERROR = f"Gemini request failed for model {model}"
    return None


def _call_openai(payload: dict[str, Any]) -> Optional[str]:
    if not OPENAI_API_KEY: return None
    prompt = _build_ai_prompt(payload)
    for model in _load_model_settings().get("openai_models", []):
        cached = _cached_ai("openai", model, payload)
        if cached: return cached

        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        }

        # Primary path: OpenAI Responses API.
        response = _request_json(
            "POST",
            "https://api.openai.com/v1/responses",
            headers=headers,
            json={
                "model": model,
                "input": prompt,
                "max_output_tokens": 420,
            },
            timeout=30,
        )
        if response:
            text = _extract_openai_response(response)
            if text:
                _store_ai("openai", model, payload, text)
                return text

        # Compatibility fallback: Chat Completions API.
        response = _request_json(
            "POST",
            "https://api.openai.com/v1/chat/completions",
            headers=headers,
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 420,
            },
            timeout=30,
        )
        if response:
            text = _extract_chat_completion(response)
            if text:
                _store_ai("openai", model, payload, text)
                return text
    return None


def _call_openai_compatible(provider: str, api_key: str, base_url: str, models: list[str], payload: dict[str, Any]) -> Optional[str]:
    if not api_key: return None
    prompt = _build_ai_prompt(payload)
    for model in models:
        cached = _cached_ai(provider, model, payload)
        if cached: return cached
        response = _request_json("POST", f"{base_url}/chat/completions",
                                 headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                                 json={"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 420}, timeout=30)
        if response:
            text = _extract_chat_completion(response)
            if text:
                _store_ai(provider, model, payload, text); return text
    return None


def _call_claude(payload: dict[str, Any]) -> Optional[str]:
    if not CLAUDE_API_KEY: return None
    prompt = _build_ai_prompt(payload)
    for model in _load_model_settings().get("claude_models", []):
        cached = _cached_ai("claude", model, payload)
        if cached: return cached
        response = _request_json("POST", "https://api.anthropic.com/v1/messages",
                                 headers={"x-api-key": CLAUDE_API_KEY, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
                                 json={"model": model, "max_tokens": 420, "messages": [{"role": "user", "content": prompt}]}, timeout=30)
        if response:
            chunks = [str(x.get("text", "")) for x in response.get("content", []) or [] if isinstance(x, dict) and x.get("type") == "text"]
            text = _clean_ai_text(" ".join(chunks))
            if text:
                _store_ai("claude", model, payload, text); return text
    return None


def generate_ai_opinions(symbol: str, price: float, rsi: float, vwap: float, ema20: float, ema50: float, atr: float,
                         vol_spike: bool, btc_trend: str, derivatives: dict[str, Any], tf_results: dict[str, str],
                         titan: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    # Bulk dashboard refresh must not wait on 5 LLM providers (often 10–40s each).
    if DASHBOARD_FAST_SCAN and not force:
        # V29: never block the market scan on an LLM, but do reuse a fresh
        # Gemini opinion produced asynchronously by the previous scan. This
        # makes AI a real evidence channel without making the dashboard wait.
        cached_ai = {}
        try:
            raw = _load_json(AI_SYMBOL_CACHE_PATH, {})
            if isinstance(raw, dict):
                row = raw.get(_normalize_symbol(symbol)) or {}
                if isinstance(row, dict) and (time.time() - safe_float(row.get("ts"), 0)) <= AUTO_AI_REFRESH_SECONDS:
                    cached_ai = row
        except Exception:
            cached_ai = {}
        if cached_ai.get("text"):
            return {
                "providers": ["gemini"],
                "gemini": cached_ai.get("text", ""),
                "internal": (titan or {}).get("summary") or "",
                "titan": (titan or {}).get("summary") or "",
                "ai_status": {"gemini": "تحلیل Gemini تازه/کش‌شده", "openai": "غیرفعال در اسکن سریع",
                              "grok": "غیرفعال در اسکن سریع", "claude": "غیرفعال در اسکن سریع",
                              "deepseek": "غیرفعال در اسکن سریع"},
                "fast_scan": True,
                "cached_at": cached_ai.get("ts"),
            }
        return {
            "providers": [],
            "internal": (titan or {}).get("summary") or "",
            "titan": (titan or {}).get("summary") or "",
            "ai_status": {
                "gemini": "در صف تحلیل پس‌زمینه",
                "openai": "غیرفعال در اسکن سریع",
                "grok": "غیرفعال در اسکن سریع",
                "claude": "غیرفعال در اسکن سریع",
                "deepseek": "غیرفعال در اسکن سریع",
            },
            "fast_scan": True,
        }
    payload = {
        "symbol": symbol, "price": price, "rsi": round(rsi, 2), "vwap": round(vwap, 8), "ema20": round(ema20, 8),
        "ema50": round(ema50, 8), "atr": round(atr, 8), "volume_spike": vol_spike, "btc_trend": btc_trend,
        "oi": derivatives.get("oi"), "oi_delta": derivatives.get("oi_delta"), "funding": derivatives.get("funding"),
        "timeframes": tf_results, "titan_bias": titan["bias"], "titan_score": titan["score"], "titan_alignment": titan["alignment"],
    }
    result: dict[str, Any] = {"providers": [], "internal": titan["summary"], "titan": titan["summary"], "ai_status": {}}
    jobs = {
        "gemini": (bool(GEMINI_API_KEY), lambda: _call_gemini(payload)),
        "openai": (bool(OPENAI_API_KEY), lambda: _call_openai(payload)),
        "grok": (bool(GROK_API_KEY), lambda: _call_openai_compatible("grok", GROK_API_KEY, "https://api.x.ai/v1", _load_model_settings().get("grok_models", []), payload)),
        "claude": (bool(CLAUDE_API_KEY), lambda: _call_claude(payload)),
        "deepseek": (bool(DEEPSEEK_API_KEY), lambda: _call_openai_compatible("deepseek", DEEPSEEK_API_KEY, "https://api.deepseek.com", _load_model_settings().get("deepseek_models", []), payload)),
    }
    for name, (enabled, _) in jobs.items(): result["ai_status"][name] = "در انتظار" if enabled else "فعال نیست"
    active = [(name, fn) for name, (enabled, fn) in jobs.items() if enabled]
    if active:
        executor = _get_ai_pool(max(3, len(active)))
        futures = {executor.submit(fn): name for name, fn in active}
        for future in as_completed(futures):
            name = futures[future]
            try:
                text = future.result(timeout=18)
                if text:
                    result[name] = text; result["providers"].append(name); result["ai_status"][name] = "تحلیل آماده"
                else:
                    result["ai_status"][name] = "فعال نیست"
            except Exception as exc:
                result["ai_status"][name] = "فعال نیست"; LOGGER.warning("AI provider %s failed: %s", name, exc)
    return result

# ============================================================
# ASSET ANALYSIS / QUALITY
# ============================================================


def _record_data_quality(symbol: str, frames: dict[str, pd.DataFrame], derivatives: dict[str, Any], macro_ok: bool = True) -> None:
    try:
        candles_ok = int(all(isinstance(frames.get(tf), pd.DataFrame) and len(frames.get(tf)) >= 10 for tf in TF_CFG))
        volume_ok = int(all("vol" in frames[tf].columns and float(frames[tf]["vol"].tail(20).sum()) > 0 for tf in TF_CFG))
        funding_ok = int(derivatives.get("funding_value") is not None)
        oi_ok = int(safe_float(derivatives.get("raw_oi"), 0) > 0)
        overall = int(candles_ok and volume_ok and (funding_ok or oi_ok) and macro_ok)
        with DB_LOCK, db_conn() as con:
            con.execute("INSERT INTO data_quality(created_at,symbol,source,latency_ms,candles_ok,volume_ok,funding_ok,oi_ok,macro_ok,overall_ok,details) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (time.time(), symbol, "Binance/CoinGlass", 0.0, candles_ok, volume_ok, funding_ok, oi_ok, int(macro_ok), overall, json.dumps({"candles": candles_ok, "volume": volume_ok, "funding": funding_ok, "oi": oi_ok, "macro": int(macro_ok)}, ensure_ascii=False)))
    except Exception as exc:
        LOGGER.info("Data quality record skipped: %s", exc)


def _create_alert(severity: str, symbol: str, category: str, message: str) -> None:
    try:
        with DB_LOCK, db_conn() as con:
            con.execute("INSERT INTO alerts(created_at,severity,symbol,category,message) VALUES(?,?,?,?,?)", (time.time(), severity, symbol, category, message))
    except Exception:
        _swallow()


def _analyze_asset_base(symbol: str, btc_trend: str, feature_only: bool = False) -> Optional[dict[str, Any]]:
    started = time.perf_counter()
    try:
        requested = {tf: (120 if tf in ("15m", "1h") else 90) for tf in TF_CFG}
        frames: dict[str, pd.DataFrame] = {}
        derivatives_box: dict[str, Any] = {"d": None}
        pool = _get_kline_pool(5)
        futs = {pool.submit(fetch_klines, symbol, tf, requested[tf]): ("kl", tf) for tf in TF_CFG}
        futs[pool.submit(fetch_derivatives, symbol)] = ("deriv", None)
        for future in as_completed(futs):
            kind, key = futs[future]
            try:
                val = future.result()
                if kind == "kl":
                    frames[key] = val
                else:
                    derivatives_box["d"] = val
            except Exception as exc:
                if kind == "kl":
                    raise
                LOGGER.debug("deriv parallel fail %s: %s", symbol, exc)
        df15, df1h = frames["15m"], frames["1h"]
        signal_close = float(df1h["close"].iloc[-1])
        # Indicators use only CLOSED candles; entry/levels use a fresh spot price when available.
        # This removes the old up-to-one-hour stale-entry problem without leaking an open candle
        # into EMA/RSI/ATR calculations.
        live_price = None
        live_timestamp = None
        live_source = ""
        norm_symbol = _normalize_symbol(symbol)
        with LIVE_LOCK:
            lp = LIVE_PRICES.get(norm_symbol) or {}
            lp_ts = safe_float(lp.get("ts"), 0.0)
            if lp_ts > 0 and time.time() - lp_ts <= 15:
                live_price = safe_float(lp.get("price"), 0) or None
                live_timestamp = lp_ts
                live_source = str(lp.get("source") or "LIVE")
        if not live_price:
            try:
                fetched_live = _fetch_live_prices([symbol]).get(norm_symbol)
            except Exception:
                fetched_live = None
            if fetched_live and fetched_live > 0:
                live_price = float(fetched_live)
                _register_live_price(symbol, live_price, "REST-single")
                with LIVE_LOCK:
                    lp_new = LIVE_PRICES.get(norm_symbol) or {}
                    live_timestamp = safe_float(lp_new.get("ts"), time.time())
                    live_source = str(lp_new.get("source") or "REST-single")
        price = float(live_price or signal_close)
        price_source = "LIVE" if live_price and live_timestamp else "CLOSED_1H"
        live_age_sec = (time.time() - live_timestamp) if live_timestamp else None
        last_closed_1h_ts = safe_float(df1h["T"].iloc[-1], 0.0) / 1000.0
        # Perf: compute each series once; defer heavy forecast until structure/regime exist
        close_1h = df1h["close"]
        rsi_series_1h = wilder_rsi(close_1h)
        rsi = float(rsi_series_1h.iloc[-1])
        atr = float(calc_atr(df1h).iloc[-1])
        vwap = calc_vwap(df15)
        ema20_s = close_1h.ewm(span=20, adjust=False).mean()
        ema50_s = close_1h.ewm(span=50, adjust=False).mean()
        ema20 = float(ema20_s.iloc[-1])
        ema50 = float(ema50_s.iloc[-1])
        vol15 = df15["vol"]
        if len(vol15) > 1:
            vol_mean = float(vol15.iloc[:-1].ewm(span=20, adjust=False).mean().iloc[-1])
        else:
            vol_mean = float(vol15.mean()) if len(vol15) else 0.0
        cur_vol = float(vol15.iloc[-1]) if len(vol15) else 0.0
        volume_spike = cur_vol > vol_mean * 1.20 if vol_mean > 0 else False
        derivatives = derivatives_box.get("d") or fetch_derivatives(symbol)
        macd_info = calc_macd(close_1h)
        stoch_info = calc_stoch_rsi(close_1h)
        pivots = calc_pivot_points(df1h)
        vol_delta = calc_volume_delta(df15, 24)
        session_info = market_session_utc(live_timestamp or last_closed_1h_ts)
        pattern_pack = detect_chart_patterns(df1h, rsi_series_1h)
        # Placeholder; full forecast runs once after structure/regime (avoids 2x path cost)
        candle_forecast = {"ok": False, "candles": [], "horizon": 12}
        structure_zones = detect_order_blocks_fvg(df1h)
        depth_snap = get_depth_snapshot(symbol)
        # Soft score nudges from advanced layers (symmetric, no long bias)
        adv_nudge = 0.0
        if macd_info.get("cross") == "bull":
            adv_nudge += 3.0
        elif macd_info.get("cross") == "bear":
            adv_nudge -= 3.0
        if macd_info.get("hist", 0) > 0:
            adv_nudge += 1.5
        elif macd_info.get("hist", 0) < 0:
            adv_nudge -= 1.5
        if stoch_info.get("zone") == "oversold":
            adv_nudge += 2.5
        elif stoch_info.get("zone") == "overbought":
            adv_nudge -= 2.5
        if vol_delta.get("delta_pct", 0) > 12:
            adv_nudge += 2.0
        elif vol_delta.get("delta_pct", 0) < -12:
            adv_nudge -= 2.0
        ls_ratio = safe_float((derivatives.get("long_short") or {}).get("long_short_ratio"), 1.0)
        if ls_ratio >= 1.6:
            adv_nudge -= 2.5  # crowded long -> mild short pressure
        elif ls_ratio <= 0.65:
            adv_nudge += 2.5  # crowded short -> mild long pressure
        taker_buy = safe_float(derivatives.get("taker_buy_pct"), float("nan"))
        if not math.isfinite(taker_buy):
            taker_buy = 50.0  # neutral only when data missing; marked unavailable upstream
        if taker_buy >= 58:
            adv_nudge += 1.5
        elif taker_buy <= 42:
            adv_nudge -= 1.5
        adv_nudge += safe_float(pattern_pack.get("score_bias"), 0)
        # Align quant score with probabilistic candle path (12-step)
        fc_bias = str((candle_forecast or {}).get("overall_bias") or "")
        fc_str = safe_float((candle_forecast or {}).get("path_strength"), 0)
        if fc_bias == "صعودی":
            adv_nudge += min(4.0, 1.2 + fc_str * 0.06)
        elif fc_bias == "نزولی":
            adv_nudge -= min(4.0, 1.2 + fc_str * 0.06)
        adv_nudge *= float(session_info.get("liquidity_boost", 1.0))
        adv_nudge = float(clamp(adv_nudge, -10.0, 10.0))

        tf_results: dict[str, str] = {}
        tf_scores: dict[str, float] = {}
        for tf in TF_CFG:
            df = frames.get(tf)
            if df is None or len(df) < 30:
                direction, score = "خنثی", 50.0
            else:
                direction, score, _ = _tf_forecast(df, tf=tf)
            score = float(clamp(safe_float(score, 50.0), 0, 100))
            tf_results[tf] = "🟢 صعودی" if direction == "صعودی" else "🔴 نزولی" if direction == "نزولی" else "⚖️ خنثی"
            tf_scores[tf] = score
        # Guaranteed complete numeric map for dashboard cards
        for tf in TF_CFG:
            tf_scores.setdefault(tf, 50.0)
        base_score = float(np.average([tf_scores[t] for t in TF_CFG], weights=[TF_CFG[t]["weight"] for t in TF_CFG]))
        btc_adjust = 5.0 if symbol != "BTC/USDT" and btc_trend == "صعودی" else -5.0 if symbol != "BTC/USDT" and btc_trend == "نزولی" else 0.0
        provisional = clamp(base_score + btc_adjust, 0, 100)
        _quant_spine_scores = {k: round(float(v), 1) for k, v in tf_scores.items()}
        _quant_spine_score = float(provisional)

        titan = build_titan_analysis(symbol=symbol, price=price, rsi=rsi, vwap=vwap, ema20=ema20, ema50=ema50,
                                     atr=atr, volume_spike=volume_spike, btc_trend=btc_trend, derivatives=derivatives,
                                     tf_results=tf_results, tf_scores=tf_scores)
        # Apply advanced technical / positioning nudge before structure refine
        score_n = int(round(clamp(safe_float(titan.get("score"), 50) + adv_nudge, 0, 100)))
        titan["score"] = score_n
        if score_n >= 55:
            titan["bias"] = "صعودی"
        elif score_n <= 45:
            titan["bias"] = "نزولی"
        else:
            titan["bias"] = "خنثی"
        bias, score_int, alignment = titan["bias"], titan["score"], titan["alignment"]
        titan["adv_layers"] = {
            "macd": macd_info, "stoch_rsi": stoch_info, "pivots": pivots,
            "volume_delta": vol_delta, "session": session_info, "nudge": round(adv_nudge, 2),
            "long_short_ratio": ls_ratio, "taker_buy_pct": taker_buy,
        }
        swing_low = float(df1h["low"].tail(12).min())
        swing_high = float(df1h["high"].tail(12).max())
        tech = technical_layers(df1h)
        fib = tech.get("fibonacci") or {}
        sr = tech.get("support_resistance") or {}
        bb = tech.get("bollinger") or {}

        # TITAN PROFESSIONAL EDGE SUITE: structure/regime first so levels & bias use them.
        structure = TITAN_EDGE_SUITE.market_structure(df1h)
        regime = TITAN_EDGE_SUITE.regime_detection(df1h, atr=atr, score=score_int)
        # V57: always compute 12-candle path so dashboard detail table/chart stay in sync
        # with live analysis (cheap relative to multi-TF + deriv; needed for UI integrity).
        try:
            candle_forecast = forecast_future_candles(
                df1h, horizon=12, patterns=pattern_pack,
                macd=macd_info, stoch=stoch_info,
                structure=structure, regime=regime,
            )
        except Exception:
            _swallow()
        liquidity = TITAN_EDGE_SUITE.liquidity_map(df15, price)
        confluence = TITAN_EDGE_SUITE.signal_confluence(
            score=score_int, alignment=alignment, rsi=rsi, price=price, vwap=vwap,
            ema20=ema20, ema50=ema50, volume_spike=volume_spike, derivatives=derivatives,
            structure=structure, regime=regime.get("regime", "unknown"))
        precision_pre = _precision_engine_snapshot(
            df15, df1h, price, atr, rsi, vwap, ema20, ema50, bias, volume_spike,
            structure, regime, derivatives)

        # V51 feature-only path: realtime publication must not execute the legacy chain
        # of meta-label/AI/neural/opportunity/learning decision engines. They remain available
        # for audit/backtest, while V49+V51 are the only live policy layers.
        if feature_only:
            dq = assess_data_quality(
                symbol=symbol, price=price, live_age_sec=live_age_sec,
                df15=df15, df1h=df1h, derivatives=derivatives, frames=frames,
            )
            _record_data_quality(symbol, frames, derivatives)
            display_side = "LONG" if score_int >= 50 else "SHORT"
            side_sign = 1 if display_side == "LONG" else -1
            rr_display = _v45_rr_levels(
                price, atr, display_side,
                float(df1h["low"].tail(12).min()) if display_side == "LONG" else float(df1h["high"].tail(12).max()),
            ) or {}
            lv_sl = _v49_num(rr_display.get("sl"),0.0)
            lv_tp1 = _v49_num(rr_display.get("tp1"),0.0)
            lv_tp2 = _v49_num(rr_display.get("tp2"),0.0)
            lv_risk = abs(price-lv_sl)
            lv_rr1 = abs(lv_tp1-price)/max(lv_risk,1e-12) if lv_tp1>0 else 0.0
            lv_rr2 = abs(lv_tp2-price)/max(lv_risk,1e-12) if lv_tp2>0 else 0.0
            feature_quality = round(clamp(
                0.40*safe_float(dq.get("score"),0)
                + 0.25*safe_float(alignment,50)
                + 0.20*safe_float(confluence.get("score"),50)
                + 0.15*safe_float(structure.get("confirmation_score"),50),
                0,100),1)
            try:
                with DB_LOCK,db_conn() as con:
                    dd_values=[safe_float(r[0]) for r in reversed(con.execute(
                        "SELECT r_multiple_24h FROM titan_v49_ledger WHERE published=1 AND outcome_24h IN ('WIN','LOSS') AND r_multiple_24h IS NOT NULL ORDER BY evaluated_at DESC LIMIT 300"
                    ).fetchall())]
                governor=TITAN_EDGE_SUITE.drawdown_governor(dd_values)
            except Exception:
                governor={"state":"UNKNOWN","risk_multiplier":1.0}
            fusion_stub={
                "data_quality":dq,"entry_ladder":{},"meta_v8":{},"neural_v9":{},
                "success_probability":None,"ai_majority":None,"ai_conflict":False,
                "explanation":["V51 feature-only realtime path; legacy decision engines bypassed"],
            }
            titan_stub=dict(titan)
            titan_stub["fusion"]=fusion_stub
            titan_stub["score"]=score_int
            return {
                "symbol":symbol,"base_symbol":symbol.split("/")[0].upper(),
                "coin_name":COIN_META.get(symbol.split("/")[0].upper(),(symbol.split("/")[0].upper(),"●"))[0],
                "coin_icon":COIN_META.get(symbol.split("/")[0].upper(),(symbol.split("/")[0].upper(),"●"))[1],
                "tv_symbol":_binance_symbol(symbol),"price":smart_format(price),"price_raw":price,
                "live_price":price if price_source=="LIVE" else None,
                "rsi":f"{rsi:.1f}","rsi_value":round(rsi,2),
                "entry_valid":smart_format(price),"tp1":smart_format(lv_tp1),"tp2":smart_format(lv_tp2),"stop_loss":smart_format(lv_sl),
                "entry_raw":price,"atr_raw":atr,"stop_loss_raw":lv_sl,"tp1_raw":lv_tp1,"tp2_raw":lv_tp2,
                "btc_trend":btc_trend,
                "tfs":dict(_quant_spine_scores),"tf_results":tf_results,"tf_scores":dict(_quant_spine_scores),
                "alignment":round(float(alignment),2),"bias":"صعودی" if score_int>=58 else "نزولی" if score_int<=42 else "خنثی",
                "score":int(round(_quant_spine_score if abs(_quant_spine_score-50)>=0.5 else score_int)),
                "score_bar":int(round(_quant_spine_score if abs(_quant_spine_score-50)>=0.5 else score_int)),
                "quant_score":int(round(_quant_spine_score if abs(_quant_spine_score-50)>=0.5 else score_int)),
                "score_color":"#4ade80" if score_int>=60 else "#f43f5e" if score_int<=40 else "#fbbf24",
                "signal_quality":feature_quality,"success_probability":None,"decision_tag":"WAIT","decision":"WAIT",
                "decision_confidence":feature_quality,"decision_state":"FEATURES_READY","signal_tag":"V51 FEATURES",
                "volume_spike":volume_spike,"volatility_pct":round(atr/max(price,1e-12)*100,3),
                "risk_distance_pct":round(lv_risk/max(price,1e-12)*100,3),"rr_tp1":round(lv_rr1,2),"rr_tp2":round(lv_rr2,2),
                "effective_rr_tp1":round(lv_rr1-V49_FRICTION_PCT/max(lv_risk/max(price,1e-12)*100,1e-9),2) if lv_risk>0 else 0.0,
                "effective_rr_tp2":round(lv_rr2-V49_FRICTION_PCT/max(lv_risk/max(price,1e-12)*100,1e-9),2) if lv_risk>0 else 0.0,
                "titan_analysis":titan_stub,"ai_opinions":{"providers":[],"internal":(titan_stub or {}).get("summary") or "","titan":(titan_stub or {}).get("summary") or "","ai_status":{"gemini":"در صف تحلیل پس‌زمینه","openai":"غیرفعال در اسکن سریع","grok":"غیرفعال در اسکن سریع","claude":"غیرفعال در اسکن سریع","deepseek":"غیرفعال در اسکن سریع"},"fast_scan":True,},
                "fusion":fusion_stub,
                "edge":{
                    "confluence":confluence,"regime":regime,"liquidity":liquidity,"structure":structure,
                    "technical":tech,"ai":{},"meta":{},"risk":{},"governor":governor,
                    "decision_tag":"WAIT","strategy_lab":{"status":"DEFERRED","reason":"diagnostic-only"},
                },
                "decision_reasons":[],"decision_architecture":{
                    "type":"FEATURE_ONLY","authoritative_source":"TITAN-V51-FINAL-GOVERNOR",
                    "legacy_decision_engines":"NOT_EXECUTED_IN_REALTIME"},
                "macd":macd_info,"macd_hist":macd_info.get("hist"),"macd_cross":macd_info.get("cross"),
                "stoch_rsi":stoch_info,"volume_delta":vol_delta,
                "session":session_info,"long_short_ratio":derivatives.get("long_short_ratio"),
                "long_ratio":derivatives.get("long_ratio"),"short_ratio":derivatives.get("short_ratio"),
                "top_long_ratio":derivatives.get("top_long_ratio"),"taker_buy_pct":derivatives.get("taker_buy_pct"),
                "taker_sell_pct":derivatives.get("taker_sell_pct"),"adv_nudge":round(adv_nudge,2),
                "patterns":pattern_pack,"candle_forecast":candle_forecast if candle_forecast.get("ok") else {"ok":False,"candles":[],"horizon":12,"status":"PENDING"},
                "btc_correlation":{},"order_blocks_fvg":structure_zones,"depth":depth_snap,
                "technical":{
                    "fibonacci":{k:smart_format(v) for k,v in fib.items()},
                    "support":smart_format(sr.get("support")) if sr.get("support") is not None else "N/A",
                    "resistance":smart_format(sr.get("resistance")) if sr.get("resistance") is not None else "N/A",
                    "bollinger":{"upper":smart_format(bb.get("upper")) if bb.get("upper") is not None else "N/A",
                                   "middle":smart_format(bb.get("middle")) if bb.get("middle") is not None else "N/A",
                                   "lower":smart_format(bb.get("lower")) if bb.get("lower") is not None else "N/A"}},
                "data_quality":dq,"param_version":TITAN_PARAM_VERSION,
                "price_source":price_source,"live_price_age_sec":round(live_age_sec,3) if live_age_sec is not None else None,
                "live_price_timestamp":live_timestamp,"closed_1h_candle_close_ms":int(df1h["T"].iloc[-1]),
                "closed_15m_candle_close_ms":int(df15["T"].iloc[-1]),
                "analysis_asof_utc":datetime.fromtimestamp(live_timestamp or last_closed_1h_ts,timezone.utc).isoformat(timespec="seconds"),
                "candle_state":"CLOSED_ONLY_INDICATORS","analysis_mode":"V51_FEATURE_ONLY",
                "forecast_accuracy":{"status":"DEFERRED"},"signal_grade":{"grade":"—","label_fa":"pending final policy","action":"WAIT","trust_index":0},
            }
        hist_wr = TITAN_EDGE_SUITE.historical_win_rate_for_item({"bias": bias})
        preliminary_quality = int(round(clamp(
            0.35 * alignment
            + 0.30 * confluence["score"]
            + 0.20 * abs(score_int - 50) * 2
            + 0.10 * (80 if volume_spike else 40)
            + 0.05 * safe_float(structure.get("confirmation_score"), 50)
            + 0.10 * safe_float(precision_pre.get("score"), 50),
            0, 100)))
        meta = TITAN_EDGE_SUITE.meta_label(confluence["score"], preliminary_quality, hist_wr)

        # Multi-layer directional refine (LONG / SHORT / WAIT)
        bias, decision_tag = refine_bias_with_edge(
            bias, score_int, alignment, structure, regime, confluence, meta)
        titan["bias"] = bias

        # Early AI lean (status-only providers skipped later); soft confirmation only
        # Full AI text is generated after levels — ensemble still runs post-AI.

        sl, tp1, tp2 = calculate_levels(
            price, atr, swing_low, swing_high, bias,
            vwap=vwap, ema20=ema20, ema50=ema50,
            structure=structure, confluence=safe_float(confluence.get("score"), 50),
            order_blocks_fvg=structure_zones, fibonacci=fib, forecast=candle_forecast,
        )
        ai = generate_ai_opinions(symbol, price, rsi, vwap, ema20, ema50, atr, volume_spike, btc_trend, derivatives, tf_results, titan)
        _record_data_quality(symbol, frames, derivatives)

        # Adaptive fusion: quant + structure + regime + confluence + AI + history
        ai_pre = TITAN_EDGE_SUITE.ai_ensemble(ai, bias)
        fusion = TITAN_ADAPTIVE.fuse_decision(
            symbol=symbol,
            quant_bias=bias,
            score=float(score_int),
            alignment=float(alignment),
            structure=structure,
            regime=regime,
            confluence=confluence,
            meta=meta,
            ai_ensemble=ai_pre,
            forecast=candle_forecast,
            patterns=pattern_pack,
        )
        prev_bias, prev_tag = bias, decision_tag
        bias = fusion.get("bias", bias)
        decision_tag = fusion.get("decision", decision_tag)
        # Recompute precision for the final fused direction; never let AI alone bypass a poor entry.
        precision = _precision_engine_snapshot(
            df15, df1h, price, atr, rsi, vwap, ema20, ema50, bias, volume_spike,
            structure, regime, derivatives)
        pscore = safe_float(precision.get("score"), 50)
        if decision_tag in {"LONG", "SHORT"}:
            # Only invalid price/ATR hard-kills. Soft precision only tags risk.
            severe_blocks = [x for x in (precision.get("hard_blocks") or []) if x in {"invalid_price_or_atr"}]
            if severe_blocks:
                decision_tag, bias = "WAIT", "خنثی"
                fusion["precision_veto"] = True
                fusion["precision_reason"] = severe_blocks
            elif pscore < 40 or "overextended_entry" in (precision.get("hard_blocks") or []):
                fusion["precision_soft"] = True
                fusion["precision_reason"] = [f"precision_soft={pscore:.0f}"]

        # --- V8 Data Quality hard gate ---
        dq = assess_data_quality(
            symbol=symbol, price=price, live_age_sec=live_age_sec,
            df15=df15, df1h=df1h, derivatives=derivatives, frames=frames,
        )
        fusion["data_quality"] = dq
        if dq.get("hard_veto") and decision_tag in {"LONG", "SHORT"}:
            decision_tag, bias = "WAIT", "خنثی"
            fusion["dq_veto"] = True
            fusion.setdefault("explanation", []).append(
                f"وتوی Data quality ({dq.get('score')}) · {', '.join((dq.get('reasons') or [])[:3])}"
            )

        # --- V8 Entry Ladder (4h/1d → 1h → 15m) ---
        # V28.4: ladder is advisory quality penalty, not a hard WAIT kill-switch.
        ladder = entry_ladder_gate(tf_scores, tf_results, decision_tag, bias)
        fusion["entry_ladder"] = ladder
        if decision_tag in {"LONG", "SHORT"} and not ladder.get("passed", True):
            fusion["ladder_soft"] = True
            fusion.setdefault("explanation", []).append(
                "نردبان ورود ناقص (نرم): " + ", ".join((ladder.get("reasons") or [])[:4])
            )

        # --- V8 Forecast path weight from tracked accuracy ---
        fc_stats = forecast_path_accuracy_stats(symbol)
        fusion["forecast_accuracy"] = fc_stats
        if decision_tag in {"LONG", "SHORT"} and candle_forecast.get("ok"):
            fc_side = candle_forecast.get("overall_bias")
            aligned = (decision_tag == "LONG" and fc_side == "صعودی") or (decision_tag == "SHORT" and fc_side == "نزولی")
            if not aligned and safe_float(fc_stats.get("weight_scale"), 0) >= 0.7 and safe_float(fc_stats.get("samples"), 0) >= 12:
                # Trusted path disagrees → soft demote to WAIT if other edges weak
                if safe_float(fusion.get("fused_score"), 0) < 0.28 and pscore < 60:
                    decision_tag, bias = "WAIT", "خنثی"
                    fusion["forecast_veto"] = True
                    fusion.setdefault("explanation", []).append("مسیر ۱۲ کندلی معتبر خلاف جهت ستاپ ضعیف")

        # --- V8 Portfolio crowding (soft: penalty only, do not erase real edges) ---
        port = portfolio_side_pressure(decision_tag, symbol)
        fusion["portfolio"] = port
        if decision_tag in {"LONG", "SHORT"} and not port.get("ok", True):
            fusion["portfolio_soft"] = True
            fusion.setdefault("explanation", []).append((port.get("reason") or "فشار سبد") + " · فقط هشدار")
            if isinstance(fusion.get("success_probability"), (int, float)):
                fusion["success_probability"] = float(clamp(
                    safe_float(fusion.get("success_probability"), 50) - 8, 5, 88
                ))
        elif port.get("penalty", 0) and isinstance(fusion.get("success_probability"), (int, float)):
            fusion["success_probability"] = float(clamp(
                safe_float(fusion.get("success_probability"), 50) - port["penalty"] * 0.35, 5, 88
            ))

        # BTC correlation filter for alts in high-vol
        atr_pct_now = (atr / price * 100) if price > 0 else 0.0
        try:
            corr_info = compute_btc_correlation(symbol)
            decision_tag, bias, corr_note = apply_btc_correlation_filter(
                symbol, decision_tag, bias, atr_pct_now, btc_trend, corr_info)
            fusion["btc_correlation"] = corr_note
        except Exception as _corr_exc:
            LOGGER.debug("corr filter skip: %s", _corr_exc)
            corr_note = {}
        # Order blocks / FVG + depth already computed once above (perf: no double work)
        if not structure_zones:
            try:
                structure_zones = detect_order_blocks_fvg(df1h)
            except Exception:
                structure_zones = {"order_blocks": [], "fvgs": [], "nearest_ob": None, "nearest_fvg": None}
        if not depth_snap:
            depth_snap = get_depth_snapshot(symbol)
        titan["bias"] = bias
        titan["fusion"] = {
            "fused_score": fusion.get("fused_score"),
            "success_probability": fusion.get("success_probability"),
            "explanation": fusion.get("explanation"),
            "ai_majority": fusion.get("ai_majority"),
            "weights": fusion.get("weights"),
            "reliability": fusion.get("reliability"),
            "thresholds": fusion.get("thresholds"),
            "prior_quant": prev_bias,
            "prior_tag": prev_tag,
        }
        # Recalculate levels if direction changed after fusion
        if bias != prev_bias or decision_tag != prev_tag:
            sl, tp1, tp2 = calculate_levels(
                price, atr, swing_low, swing_high, bias,
                vwap=vwap, ema20=ema20, ema50=ema50,
                structure=structure, confluence=safe_float(confluence.get("score"), 50),
                order_blocks_fvg=structure_zones, fibonacci=fib, forecast=candle_forecast,
            )
        # Soft score nudge toward fused conviction (display only, clamped)
        try:
            fused_nudge = int(round(safe_float(fusion.get("fused_score"), 0) * 8))
            score_int = int(clamp(score_int + fused_nudge, 0, 100))
            titan["score"] = score_int
        except Exception:
            _swallow()

        volatility_pct = atr / price * 100 if price > 0 else 0.0
        risk_distance_pct = abs(price - sl) / price * 100 if price > 0 else 0.0
        rr_tp1 = abs(tp1 - price) / max(abs(price - sl), 1e-12)
        rr_tp2 = abs(tp2 - price) / max(abs(price - sl), 1e-12)

        ai_consensus = TITAN_EDGE_SUITE.ai_ensemble(ai, bias)
        drawdown_values = []
        try:
            with DB_LOCK, db_conn() as con:
                drawdown_values = [safe_float(r[0]) for r in con.execute("SELECT r_multiple FROM paper_trades WHERE status='CLOSED' AND r_multiple IS NOT NULL ORDER BY closed_at DESC LIMIT 300").fetchall()][::-1]
        except Exception:
            drawdown_values = []
        governor = TITAN_EDGE_SUITE.drawdown_governor(drawdown_values)
        adaptive = TITAN_EDGE_SUITE.adaptive_risk(
            base_risk=1.0, volatility_pct=volatility_pct,
            confidence=meta["probability"], regime=regime.get("regime", "unknown"),
            drawdown_pct=governor.get("drawdown_pct", 0))
        risk = {**adaptive, "governor_state": governor.get("state"), "governor_multiplier": governor.get("risk_multiplier")}
        # Professional composite quality: alignment + confluence + meta + AI agreement + structure
        ai_agree = safe_float(ai_consensus.get("agreement"), 0)
        signal_quality = int(round(clamp(
            preliminary_quality * 0.45
            + confluence["score"] * 0.20
            + meta["probability"] * 0.15
            + ai_agree * 0.10
            + safe_float(structure.get("confirmation_score"), 50) * 0.07
            + pscore * 0.15,
            0, 100)))
        if governor.get("state") == "DEFENSIVE":
            signal_quality = min(signal_quality, 62)
        if decision_tag == "WAIT":
            signal_quality = min(signal_quality, 70)
        # Blend adaptive success probability into quality
        success_prob = safe_float((titan.get("fusion") or {}).get("success_probability"), 50)
        # Precision is an entry-quality correction, not a claim of calibrated probability.
        success_prob = float(clamp(0.88 * success_prob + 0.12 * pscore, 5, 88))
        try:
            cal_dir = "صعودی" if decision_tag == "LONG" else "نزولی" if decision_tag == "SHORT" else ""
            cal_pack = calibrate_success_probability_v8(
                symbol, cal_dir, success_prob, regime=str((regime or {}).get("regime", "")),
            )
            success_prob = float(clamp(safe_float(cal_pack.get("calibrated"), success_prob), 5, 88))
            if isinstance(titan.get("fusion"), dict):
                titan["fusion"]["success_probability"] = success_prob
                titan["fusion"]["probability_calibration"] = cal_pack
                titan["fusion"]["param_version"] = TITAN_PARAM_VERSION
        except Exception as _cal_exc:
            LOGGER.debug("post cal failed: %s", _cal_exc)
            cal_pack = {}
        # V8 meta-label final accept/reject
        try:
            stretch = max(
                safe_float((precision or {}).get("stretch_vwap_atr"), 0),
                safe_float((precision or {}).get("stretch_ema_atr"), 0),
            )
            meta_v8 = meta_label_v8(
                confluence_score=safe_float(confluence.get("score"), 50),
                precision_score=pscore,
                alignment=float(alignment),
                success_prob=success_prob,
                ai_conflict=bool((titan.get("fusion") or {}).get("ai_conflict")),
                stretch_atr=stretch,
                ladder_passed=bool((fusion.get("entry_ladder") or {}).get("passed", True)),
                data_quality=safe_float((fusion.get("data_quality") or {}).get("score"), 50),
                hist_wr=safe_float(hist_wr, 50) if not isinstance(hist_wr, dict) else safe_float(hist_wr.get("win_rate"), 50),
            )
            fusion["meta_v8"] = meta_v8
            titan["fusion"]["meta_v8"] = meta_v8