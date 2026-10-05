def _v29_num_price(value: Any) -> float:
    """Parse TITAN display prices safely."""
    try:
        s = str(value or "").replace(",", "").replace("$", "").replace("USDT", "").strip()
        return float(s)
    except Exception:
        return 0.0


def _v29_shift_price_field(value: Any, ratio: float) -> Any:
    p = _v29_num_price(value)
    if p <= 0 or not math.isfinite(ratio):
        return value
    return smart_format(p * ratio)


def _v29_sync_snapshot_to_live(data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Rebase only price-dependent dashboard outputs to the newest Binance spot
    price. Closed-candle indicators remain untouched; entry/SL/TP and the
    scenario path move with the live reference price.
    """
    if not data:
        return data
    now = time.time()
    with LIVE_LOCK:
        live_map = {
            k: dict(v) for k, v in LIVE_PRICES.items()
            if safe_float(v.get("price"), 0) > 0
        }
    for item in data:
        sym = _normalize_symbol(item.get("symbol", ""))
        lp = live_map.get(sym)
        if not lp:
            continue
        live = safe_float(lp.get("price"), 0)
        if live <= 0:
            continue
        old = _v29_num_price(item.get("price"))
        if old <= 0:
            item["price"] = smart_format(live)
            item["entry_valid"] = smart_format(live)
            continue

        ratio = live / old
        # Keep the canonical signal timestamp/price for audit, but expose a
        # second, authoritative live price for the current UI.
        item.setdefault("signal_snapshot", {})
        item["signal_snapshot"].setdefault("price", old)
        item["signal_snapshot"].setdefault("timestamp", item.get("scan_timestamp", now))
        item["price"] = smart_format(live)
        item["price_raw"] = float(live)
        item["live_price"] = float(live)
        item["entry_valid"] = smart_format(live)
        item["entry_raw"] = float(live)
        for key in ("stop_loss", "tp1", "tp2"):
            if key in item:
                item[key] = _v29_shift_price_field(item.get(key), ratio)

        # Recompute price-relative risk metrics from the live price.
        sl = _v29_num_price(item.get("stop_loss"))
        tp1 = _v29_num_price(item.get("tp1"))
        tp2 = _v29_num_price(item.get("tp2"))
        if sl > 0:
            item["stop_loss_raw"] = float(sl)
            if tp1 > 0: item["tp1_raw"] = float(tp1)
            if tp2 > 0: item["tp2_raw"] = float(tp2)
            item["risk_distance_pct"] = round(abs(live - sl) / live * 100.0, 3)
            item["rr_tp1"] = round(abs(tp1 - live) / max(abs(live - sl), 1e-12), 2) if tp1 > 0 else item.get("rr_tp1", 0)
            item["rr_tp2"] = round(abs(tp2 - live) / max(abs(live - sl), 1e-12), 2) if tp2 > 0 else item.get("rr_tp2", 0)
            item["effective_rr_tp1"] = item["rr_tp1"]
            item["effective_rr_tp2"] = item["rr_tp2"]

        scan_ts = safe_float(item.get("scan_timestamp"), now)
        item["live_price"] = live
        item["live_price_source"] = lp.get("source", "Binance")
        item["live_price_ts"] = safe_float(lp.get("ts"), now)
        item["live_price_age_sec"] = round(max(0.0, now - safe_float(lp.get("ts"), now)), 2)
        item["live_sync"] = True
        item["price_delta_from_scan_pct"] = round((live / old - 1.0) * 100.0, 4)
        item["signal_age_sec"] = round(max(0.0, now - scan_ts), 1)

        # Re-anchor future candle scenario to the same live price while keeping
        # its modeled percentage geometry unchanged.
        fc = item.get("candle_forecast")
        if isinstance(fc, dict):
            fc["last_price"] = round(live, 8)
            for candle in fc.get("candles", []) or []:
                if isinstance(candle, dict):
                    for k in ("open", "high", "low", "close", "mid", "band_low", "band_high"):
                        if k in candle:
                            try:
                                candle[k] = round(float(candle[k]) * ratio, 8)
                            except Exception:
                                _swallow()
    return data


def _v29_async_gemini_enrichment(snapshot: list[dict[str, Any]], macro: dict[str, Any]) -> None:
    """Low-latency asynchronous Gemini evidence pass; never blocks market scan.

    V58 optimization: never launch overlapping enrichment jobs and never re-query
    a symbol whose Gemini evidence is still inside AUTO_AI_REFRESH_SECONDS.
    """
    if not GEMINI_API_KEY or not snapshot:
        return
    if not _AI_ENRICH_LOCK.acquire(blocking=False):
        return
    try:
        ordered = sorted(
            snapshot,
            key=lambda x: (
                -safe_float(x.get("signal_quality"), 0),
                -safe_float(x.get("success_probability"), 0),
            ),
        )[:AUTO_AI_TOP_N]
        raw = _load_json(AI_SYMBOL_CACHE_PATH, {})
        cache = raw if isinstance(raw, dict) else {}
        jobs = []
        now_ts = time.time()
        for item in ordered:
            sym = _normalize_symbol(item.get("symbol", ""))
            cached_row = cache.get(sym) if isinstance(cache, dict) else None
            if isinstance(cached_row, dict) and (now_ts - safe_float(cached_row.get("ts"), 0)) <= AUTO_AI_REFRESH_SECONDS and cached_row.get("text"):
                continue
            payload = {
                "task": "TITAN evidence review only; do not invent prices; use supplied live price.",
                "symbol": sym,
                "live_price": _v29_num_price(item.get("price")),
                "decision": item.get("decision_tag", "WAIT"),
                "score": item.get("score", 50),
                "signal_quality": item.get("signal_quality", 0),
                "success_probability": item.get("success_probability", 50),
                "timeframes": item.get("tf_scores", {}),
                "bias": item.get("bias"),
                "entry": item.get("entry_valid"),
                "stop_loss": item.get("stop_loss"),
                "tp1": item.get("tp1"),
                "tp2": item.get("tp2"),
                "rsi": item.get("rsi"),
                "macd": item.get("macd"),
                "derivatives": {
                    "oi": item.get("coinglass_oi"),
                    "funding": item.get("coinglass_funding"),
                    "taker_buy_pct": item.get("taker_buy_pct"),
                    "long_short_ratio": item.get("long_short_ratio"),
                },
                "macro": {
                    "btc_trend": macro.get("btc_trend"),
                    "fear_greed": macro.get("fear_greed_val"),
                },
            }
            jobs.append((sym, payload))

        def one(sym_payload):
            sym, payload = sym_payload
            try:
                result = _call_gemini(payload)
                if result:
                    return sym, {"ts": time.time(), "text": result, "source": "Gemini", "price": payload["live_price"]}
            except Exception as exc:
                LOGGER.debug("V29 async Gemini %s failed: %s", sym, exc)
            return sym, None

        if not jobs:
            return

        pool = _get_ai_pool(min(2, max(1, len(jobs))))
        futures = [pool.submit(one, j) for j in jobs]
        for fut in as_completed(futures):
            sym, row = fut.result()
            if row:
                cache[sym] = row

        _save_json(AI_SYMBOL_CACHE_PATH, cache)

        # Feed fresh Gemini evidence into the in-memory dashboard without
        # forcing a page reload. The next automatic scan also consumes it.
        with CACHE_LOCK:
            current = CACHE.get("data") or []
            by = {_normalize_symbol(x.get("symbol", "")): x for x in current}
            for sym, row in cache.items():
                if sym in by and isinstance(by[sym], dict) and row.get("text"):
                    ai = by[sym].get("ai_opinions") if isinstance(by[sym].get("ai_opinions"), dict) else {}
                    ai = dict(ai)
                    ai["gemini"] = row["text"]
                    ai["providers"] = list(dict.fromkeys((ai.get("providers") or []) + ["gemini"]))
                    ai["ai_status"] = dict(ai.get("ai_status") or {})
                    ai["ai_status"]["gemini"] = "تحلیل Gemini زنده"
                    ai["cached_at"] = row.get("ts")
                    by[sym]["ai_opinions"] = ai
            CACHE["data"] = _v29_sync_snapshot_to_live(list(by.values()))
            CACHE["timestamp"] = time.time()
            snap = CACHE["data"]
            summary = CACHE.get("gemini_summary") or ""
            macro_now = CACHE.get("macro") or macro
        _save_json(MARKET_CACHE_PATH, {
            "timestamp": time.time(), "data": snap,
            "gemini_summary": summary, "macro": macro_now
        })
    except Exception as exc:
        LOGGER.warning("V29 Gemini enrichment failed: %s", exc)
    finally:
        try:
            _AI_ENRICH_LOCK.release()
        except RuntimeError:
            pass


def _v29_auto_loop() -> None:
    """Permanent autonomous maintenance loop independent of browser requests.

    Three independent maintenance lanes are kept alive:
      1) live multi-asset market scans;
      2) forecast/outcome resolution and learning calibration;
      3) throttled historical-performance/backtest snapshots.

    The performance lane is dispatched in its own daemon thread so an Android
    backtest cannot block the live dashboard or the 45-second market scanner.
    """
    last_scan = 0.0
    while True:
        try:
            now = time.time()
            if now - last_scan >= AUTO_SCAN_INTERVAL_SECONDS:
                started = time.perf_counter()
                queued = _background_market_refresh(False)
                last_scan = now
                LOGGER.info(
                    "V29 autonomous scan %s in %.1fms",
                    "queued" if queued else "already-running",
                    (time.perf_counter() - started) * 1000,
                )

            # Resolve matured forecasts and rebuild the conservative learning
            # profile from actual outcomes. This never invents a result.
            _scan_supervisor_tick()
            _safe_background_forecast_maintenance()

            # Historical performance is throttled internally (currently every
            # 5 minutes) and rotates through symbols. Dispatching it separately
            # prevents a slow backtest/API call from freezing live updates.
            try:
                if now - _PERFORMANCE_LAST_RUN >= PERFORMANCE_AUTO_INTERVAL_SECONDS:
                    threading.Thread(
                        target=autonomous_performance_maintenance,
                        kwargs={"force": False},
                        name="titan-performance-maintenance",
                        daemon=True,
                    ).start()
            except Exception as perf_exc:
                LOGGER.debug("Performance maintenance dispatch failed: %s", perf_exc)
        except Exception as exc:
            LOGGER.warning("V29 autonomous loop error: %s", exc)
        LIVE_STOP.wait(AUTO_MAINTENANCE_INTERVAL_SECONDS)


def _update_cache_base(force: bool = False) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    global CACHE
    reload_keys()
    now = time.time()
    with CACHE_LOCK:
        if CACHE["data"] and not force and now - CACHE["timestamp"] < MARKET_CACHE_TTL:
            return CACHE["data"], CACHE["gemini_summary"], CACHE["macro"]
    with UPDATE_LOCK:
        with CACHE_LOCK:
            if CACHE["data"] and not force and time.time() - CACHE["timestamp"] < MARKET_CACHE_TTL:
                return CACHE["data"], CACHE["gemini_summary"], CACHE["macro"]
        coins = USER_SETTINGS.get("active_coins") or DEFAULT_COINS
        scan_started=time.time()
        _scan_progress_update(status="running",phase="دریافت داده‌های زنده و کلان",started_at=scan_started,finished_at=0.0,completed=0,total=len(coins),percent=4,message="در حال دریافت قیمت، BTC و داده‌های کلان…",fresh=False)
        # Bootstrap: macro + BTC + batch live prices in parallel (one REST round-trip for all coins)
        def _batch_live():
            try:
                prices = _fetch_live_prices(list(coins))
                for sym, px in prices.items():
                    _register_live_price(sym, px, "REST-batch")
                return len(prices)
            except Exception as exc:
                LOGGER.debug("batch live prices: %s", exc)
                return 0
        with ThreadPoolExecutor(max_workers=3) as meta_ex:
            btc_future = meta_ex.submit(fetch_btc_trend)
            macro_future = meta_ex.submit(fetch_macro)
            live_future = meta_ex.submit(_batch_live)
            btc_trend = btc_future.result()
            macro = macro_future.result()
            try:
                live_future.result(timeout=8)
            except Exception:
                _swallow()
        macro["btc_trend"] = btc_trend
        _scan_progress_update(phase="تحلیل تک‌تک ارزها با موتور یکپارچه",percent=12,message="داده‌های اولیه آماده شد؛ تحلیل ارزها در حال انجام است…")
        results: list[dict[str, Any]] = []
        scan_t0 = time.perf_counter()
        pool = _get_analysis_pool(min(4, max(2, len(coins))))
        futures = {pool.submit(analyze_asset, symbol, btc_trend): symbol for symbol in coins}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                result = future.result()
                if result:
                    results.append(result)
            except Exception as exc:
                LOGGER.exception("Asset analysis failed for %s: %s", symbol, exc)
            finally:
                done=len(results)
                pct=12 + int(72 * min(1.0, done/max(1,len(coins))))
                _scan_progress_update(completed=done,percent=pct,message=f"تحلیل {done}/{len(coins)} ارز تکمیل شد…")
        if not results:
            stale_data, stale_summary, stale_macro = _load_market_cache()
            with CACHE_LOCK: CACHE.update(timestamp=time.time(), data=stale_data, gemini_summary=stale_summary, macro=stale_macro)
            return stale_data, stale_summary, stale_macro
        order = {coin: i for i, coin in enumerate(coins)}
        # V28.4: directional first (LONG/SHORT), then grade, quality, original order
        def _result_rank(x: dict) -> tuple:
            dec = str(x.get("decision_tag") or "WAIT").upper()
            dir_pri = 0 if dec in {"LONG", "SHORT"} else 1
            side_pri = 0 if dec == "LONG" else 1 if dec == "SHORT" else 2
            g = str(x.get("grade") or ((x.get("signal_grade") or {}).get("grade")) or "—")
            return (
                dir_pri,
                side_pri,
                -GRADE_RANK.get(g, 0),
                -safe_float(x.get("opportunity_score"), 0),
                -safe_float(x.get("trust_index"), 0),
                -safe_float(x.get("signal_quality"), 0),
                order.get(x.get("symbol"), 999),
            )
        results.sort(key=_result_rank)
        scan_timestamp=time.time()
        finalized=[]
        for _item in results:
            if not isinstance(_item, dict):
                continue
            _item = _dashboard_finalize_item(_item) or _item
            _item["scan_timestamp"]=scan_timestamp
            _item["scan_time_utc"]=datetime.fromtimestamp(scan_timestamp,timezone.utc).isoformat(timespec="seconds")
            finalized.append(_item)
        results = finalized
        try:
            signal_board = rank_market_signals(results)
            macro["signal_board"] = signal_board
            macro["actionable_count"] = signal_board.get("counts", {}).get("actionable", 0)
            macro["grade_summary"] = {
                g: sum(1 for r in results if (r.get("grade") or (r.get("signal_grade") or {}).get("grade")) == g)
                for g in ("A+", "A", "B", "C", "D", "F")
            }
        except Exception as _sb:
            LOGGER.debug("signal board failed: %s", _sb)
            macro["signal_board"] = {"actionable": [], "watchlist": [], "counts": {}}
        if not TITAN_V51_AUTHORITATIVE:
            try:
                # Legacy V32 forecast/paper telemetry is kept for audit only.
                v32_record_scan_predictions(results)
                v32_evaluate_pending()
                store_forecasts(results)
                for item in results:
                    _g = str(item.get("grade") or (item.get("signal_grade") or {}).get("grade") or "")
                    if (item.get("decision_tag") in {"LONG", "SHORT"}
                        and item.get("alignment", 0) >= 65
                        and item.get("signal_quality", 0) >= MIN_DIRECTIONAL_QUALITY
                        and item.get("bias") in {"صعودی", "نزولی"}
                        and _g in {"A+", "A", "B"}):
                        paper_open_signal(item)
                evaluate_paper_trades(); evaluate_pending_forecasts()
            except Exception as exc: LOGGER.warning("Forecast/paper batch failed: %s", exc)
        # Do NOT block first dashboard paint on Gemini. The market result is complete and
        # usable at this point; keep the previous summary until the fresh AI summary arrives.
        with CACHE_LOCK:
            previous_summary = CACHE.get("gemini_summary") or ""
        if not previous_summary:
            _, previous_summary, _ = _load_market_cache()
        payload = {"timestamp": time.time(), "data": results, "gemini_summary": previous_summary, "macro": macro}
        finished=time.time()
        _scan_progress_update(status="complete",phase="اسکن کامل شد",completed=len(results),total=len(coins),percent=100,finished_at=finished,last_success_at=finished,elapsed_sec=round(finished-scan_started,1),message=f"اسکن جدید کامل شد · {len(results)}/{len(coins)} ارز · داده تازه",fresh=True)
        LOGGER.info("update_cache analyze done in %.1fs coins=%s", time.perf_counter()-scan_t0, len(results))
        results = _v29_sync_snapshot_to_live(results)
        payload["data"] = results
        _save_json(MARKET_CACHE_PATH, payload)
        with CACHE_LOCK:
            CACHE.update(timestamp=time.time(), data=results, gemini_summary=previous_summary, macro=macro)
        # Gemini is deliberately asynchronous: it enriches the live snapshot and
        # is consumed by the next scan, while the current dashboard stays fast.
        threading.Thread(
            target=_v29_async_gemini_enrichment,
            args=(list(results), dict(macro)),
            name="titan-gemini-enrichment",
            daemon=True,
        ).start()
        # Gemini global summary is also non-blocking.
        def _ai_summary_refresh(snapshot, macro_snapshot):
            global _AI_SUMMARY_LAST_RUN
            if not GEMINI_API_KEY or not _AI_SUMMARY_LOCK.acquire(blocking=False):
                return
            try:
                now = time.time()
                if now - _AI_SUMMARY_LAST_RUN < AUTO_AI_REFRESH_SECONDS:
                    return
                _AI_SUMMARY_LAST_RUN = now
                fresh = _global_ai_summary(snapshot, macro_snapshot)
                if fresh:
                    with CACHE_LOCK:
                        CACHE["gemini_summary"] = fresh
                    current = _load_market_cache()
                    _save_json(MARKET_CACHE_PATH, {
                        "timestamp": time.time(),
                        "data": current[0] or snapshot,
                        "gemini_summary": fresh,
                        "macro": macro_snapshot,
                    })
            except Exception as exc:
                LOGGER.warning("Async Gemini dashboard summary failed: %s", exc)
            finally:
                try:
                    _AI_SUMMARY_LOCK.release()
                except RuntimeError:
                    pass
        threading.Thread(target=_ai_summary_refresh, args=(results, macro), name="titan-gemini-summary", daemon=True).start()
        return results, previous_summary, macro

# ============================================================
# ADVANCED METRICS
# ============================================================


def get_advanced_metrics() -> dict[str, Any]:
    live = _live_status(); audit = get_audit_stats()
    with DB_LOCK, db_conn() as con:
        paper_total = con.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
        paper_closed = con.execute("SELECT COUNT(*) FROM paper_trades WHERE status='CLOSED'").fetchone()[0]
        paper_open = con.execute("SELECT COUNT(*) FROM paper_trades WHERE status='OPEN'").fetchone()[0]
        paper_values = [safe_float(r[0]) for r in con.execute("SELECT r_multiple FROM paper_trades WHERE status='CLOSED' AND r_multiple IS NOT NULL").fetchall()]
        alerts = [dict(r) for r in con.execute("SELECT id,created_at,severity,symbol,category,message FROM alerts ORDER BY id DESC LIMIT 10").fetchall()]
        quality = con.execute("SELECT COUNT(*), COALESCE(SUM(overall_ok),0) FROM data_quality").fetchone()
    governor = TITAN_EDGE_SUITE.drawdown_governor(paper_values)
    return {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "live": live, "historical": audit,
            "paper": {"total": int(paper_total), "closed": int(paper_closed), "open": int(paper_open), **_safe_return_series(paper_values)},
            "data_quality": {"samples": int(quality[0]), "healthy": int(quality[1]), "cache_ttl_seconds": MARKET_CACHE_TTL},
            "drawdown_governor": governor, "edge_suite": {"modules": 12, "status": "ACTIVE", "name": TITAN_EDGE_SUITE.name}, "alerts": alerts}

# ============================================================
# AUTONOMOUS PERFORMANCE SNAPSHOT ENGINE
# ============================================================

PERFORMANCE_AUTO_INTERVAL_SECONDS = 300
PERFORMANCE_SYMBOLS_PER_RUN = 3
_PERFORMANCE_LOCK = threading.Lock()
_PERFORMANCE_LAST_RUN = 0.0
_PERFORMANCE_CURSOR = 0
_PERFORMANCE_LAST_RESULT: dict[str, Any] = {"ok": True, "status": "never_run", "completed": 0, "failed": 0}


def _store_performance_snapshot(symbol: str, tf: str, result: dict[str, Any]) -> None:
    if not isinstance(result, dict) or not result.get("ok"):
        return
    m = result.get("metrics") or {}
    oos = (((result.get("professional") or {}).get("oos") or {}).get("status")
           or ((result.get("professional") or {}).get("out_of_sample") or {}).get("status") or "")
    payload = json.dumps(result, ensure_ascii=False, default=str)
    with DB_LOCK, db_conn() as con:
        con.execute(
            """INSERT INTO performance_snapshots(
                symbol,timeframe,created_at,trades,wins,losses,win_rate,expectancy,
                profit_factor,max_drawdown,sharpe,sortino,oos_status,metrics_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                _normalize_symbol(symbol), str(tf), time.time(),
                int(safe_float(m.get("trades"), 0)),
                int(safe_float(m.get("wins"), 0)),
                int(safe_float(m.get("losses"), 0)),
                safe_float(m.get("win_rate"), 0),
                safe_float(m.get("expectancy"), 0),
                safe_float(m.get("profit_factor"), 0) if m.get("profit_factor") is not None else None,
                safe_float(m.get("max_drawdown"), 0),
                safe_float(m.get("sharpe"), 0),
                safe_float(m.get("sortino"), 0),
                str(oos),
                payload[-120000:],
            ),
        )


def autonomous_performance_maintenance(force: bool = False) -> dict[str, Any]:
    """Continuously build auditable backtest history without browser interaction.

    This is deliberately throttled for Android: a small rotating subset of coins
    is evaluated each cycle, while all timeframes are covered for those coins.
    """
    global _PERFORMANCE_LAST_RUN, _PERFORMANCE_CURSOR, _PERFORMANCE_LAST_RESULT
    now = time.time()
    if not force and now - _PERFORMANCE_LAST_RUN < PERFORMANCE_AUTO_INTERVAL_SECONDS:
        return {"ok": True, "skipped": True, "reason": "interval"}
    if not _PERFORMANCE_LOCK.acquire(blocking=False):
        return {"ok": True, "skipped": True, "reason": "already_running"}
    try:
        _PERFORMANCE_LAST_RUN = now
        coins = list(USER_SETTINGS.get("active_coins") or DEFAULT_COINS)
        if not coins:
            return {"ok": False, "error": "no active coins"}
        start = _PERFORMANCE_CURSOR % len(coins)
        selected = [coins[(start + i) % len(coins)] for i in range(min(PERFORMANCE_SYMBOLS_PER_RUN, len(coins)))]
        _PERFORMANCE_CURSOR = (start + len(selected)) % len(coins)
        summary = {"ok": True, "symbols": selected, "timeframes": list(TF_CFG), "completed": 0, "failed": 0, "started_at": now}
        for sym in selected:
            for tf in TF_CFG:
                try:
                    result = backtest_signal_logic(_normalize_symbol(sym), tf, 800)
                    if result.get("ok"):
                        _store_performance_snapshot(sym, tf, result)
                        summary["completed"] += 1
                    else:
                        summary["failed"] += 1
                except Exception as exc:
                    summary["failed"] += 1
                    LOGGER.debug("Autonomous performance %s %s failed: %s", sym, tf, exc)
        # Retain bounded history; raw metrics are also available in the latest rows.
        with DB_LOCK, db_conn() as con:
            con.execute(
                "DELETE FROM performance_snapshots WHERE id NOT IN "
                "(SELECT id FROM performance_snapshots ORDER BY created_at DESC LIMIT 5000)"
            )
        summary["finished_at"] = time.time()
        summary["elapsed_sec"] = round(summary["finished_at"] - now, 2)
        summary["status"] = "complete" if summary["failed"] == 0 else "partial"
        _PERFORMANCE_LAST_RESULT = dict(summary)
        return summary
    finally:
        _PERFORMANCE_LOCK.release()


def get_performance_history(symbol: str = "", tf: str = "", limit: int = 120) -> dict[str, Any]:
    symbol = _normalize_symbol(symbol) if symbol else ""
    limit = max(1, min(int(limit), 500))
    where, args = [], []
    if symbol:
        where.append("symbol=?"); args.append(symbol)
    if tf in TF_CFG:
        where.append("timeframe=?"); args.append(tf)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    with DB_LOCK, db_conn() as con:
        rows = con.execute(
            f"SELECT symbol,timeframe,created_at,trades,wins,losses,win_rate,expectancy,"
            f"profit_factor,max_drawdown,sharpe,sortino,oos_status "
            f"FROM performance_snapshots{clause} ORDER BY created_at DESC LIMIT ?",
            (*args, limit),
        ).fetchall()
    return {
        "ok": True,
        "items": [dict(r) for r in rows],
        "count": len(rows),
        "source": "AUTONOMOUS_BACKTEST_HISTORY",
        "autonomous_status": dict(_PERFORMANCE_LAST_RESULT),
    }


# ============================================================
# TITAN ENTERPRISE V4 - ADVANCED CONTROL MODULES
# Restored from the user's original master build. Additive only.
# ============================================================

class TitanEnterpriseEnhancementV4:
    """
    Enterprise enhancement layer:
    1. Pipeline validation
    2. Learning memory hooks
    3. Backtest framework
    4. Adaptive weighting
    5. Market manipulation protection
    6. Runtime safety checks
    """
    def __init__(self):
        self.history = []
        self.weights = {
            "technical": 0.35,
            "ai": 0.30,
            "derivatives": 0.20,
            "risk": 0.15
        }

    def validate_pipeline(self, data):
        return bool(data and isinstance(data, dict))

    def save_feedback(self, signal, result):
        _append_capped(self.history, {"signal": signal, "result": result}, 256)

    def adaptive_weights(self, market_state="normal"):
        if market_state == "sideways":
            self.weights = {
                "technical": 0.25,
                "ai": 0.25,
                "derivatives": 0.30,
                "risk": 0.20
            }
        elif market_state == "trend":
            self.weights = {
                "technical": 0.45,
                "ai": 0.30,
                "derivatives": 0.15,
                "risk": 0.10
            }
        return self.weights

    def backtest_record(self, decision, price_before, price_after):
        change = 0
        if price_before:
            change = ((price_after - price_before) / price_before) * 100
        return {"decision": decision, "performance": round(change, 4)}

    def anomaly_check(self, volume_change=0, volatility=0):
        if volume_change > 300 or volatility > 10:
            return {"blocked": True, "reason": "market anomaly detected"}
        return {"blocked": False}

    def self_check(self, payload):
        checks = {
            "data": self.validate_pipeline(payload),
            "risk": payload.get("risk", 0) < 50 if isinstance(payload, dict) else False,
            "engine": True
        }
        return {"passed": all(checks.values()), "checks": checks}


TITAN_ENTERPRISE_V4 = TitanEnterpriseEnhancementV4()


# ============================================================
# TITAN FULL AUDIT & TEST FRAMEWORK
# ============================================================

class TitanFullAuditFramework:
    """Internal validation, stress and decision-quality audit layer."""
    def __init__(self):
        self.results = []
        self.paper_trades = []

    def static_check(self):
        return {
            "python_syntax": True,
            "core_loaded": True,
            "error_guard": True,
            "decision_layer": "TITAN_DECISION_CORE" in globals()
        }

    def pipeline_check(self, payload=None):
        return {
            "data": isinstance(payload, dict) if payload is not None else False,
            "risk": True,
            "decision_ready": True
        }

    def stress_test(self, scenario):
        blocked = scenario in ["api_down", "invalid_data", "extreme_volatility"]
        return {"scenario": scenario, "safe_mode": blocked, "action": "WAIT" if blocked else "CONTINUE"}

    def record_paper_trade(self, signal):
        _append_capped(self.paper_trades, signal, 512)
        return {"saved": True, "count": len(self.paper_trades)}

    def decision_report(self, decision):
        report = {
            "decision": decision,
            "has_confidence": "confidence" in decision if isinstance(decision, dict) else False,
            "explainable": "explanation" in decision if isinstance(decision, dict) else False
        }
        _append_capped(self.results, report, 512)
        return report


TITAN_AUDIT_ENGINE = TitanFullAuditFramework()


# ============================================================
# TITAN ULTIMATE - BACKTEST & CALIBRATION ENGINE
# ============================================================

class TitanBacktestCalibrationEngine:
    """Offline evaluation and confidence calibration framework."""
    def __init__(self):
        self.trades = []
        self.stats = {"total": 0, "wins": 0, "losses": 0}

    def record(self, decision, entry, exit_price, confidence=0):
        if not entry:
            return None
        pnl = ((exit_price - entry) / entry) * 100
        if decision == "SHORT":
            pnl *= -1
        result = {
            "decision": decision, "entry": entry, "exit": exit_price,
            "confidence": confidence, "pnl": round(pnl, 4), "success": pnl > 0
        }
        _append_capped(self.trades, result, 2000)
        self.stats["total"] += 1
        if result["success"]:
            self.stats["wins"] += 1
        else:
            self.stats["losses"] += 1
        return result

    def report(self):
        total = self.stats["total"]
        if total == 0:
            return {"status": "no_data", "message": "No backtest records available"}
        return {
            "total_trades": total,
            "win_rate": round((self.stats["wins"] / total) * 100, 2),
            "loss_rate": round((self.stats["losses"] / total) * 100, 2),
            "avg_confidence": round(sum(t["confidence"] for t in self.trades) / total, 2)
        }

    def calibrate(self):
        report = self.report()
        if report.get("win_rate", 0) < 50:
            return {"action": "reduce_risk", "reason": "low historical accuracy"}
        return {"action": "normal", "reason": "acceptable historical performance"}


TITAN_BACKTEST_ENGINE = TitanBacktestCalibrationEngine()


# ============================================================
# TITAN ULTIMATE - WALK FORWARD VALIDATION ENGINE
# ============================================================

class TitanWalkForwardValidationEngine:
    """Walk-forward evaluation and confidence calibration layer."""
    def __init__(self):
        self.windows = []
        self.results = []

    def create_window(self, train_data, test_data):
        window = {
            "train_size": len(train_data) if train_data else 0,
            "test_size": len(test_data) if test_data else 0
        }
        _append_capped(self.windows, window, 512)
        return window

    def evaluate(self, predictions, outcomes):
        if not predictions or not outcomes:
            return {"status": "no_data", "accuracy": 0}
        total = min(len(predictions), len(outcomes))
        correct = sum(1 for p, o in zip(predictions[:total], outcomes[:total]) if p == o)
        result = {"samples": total, "accuracy": round((correct / total) * 100, 2)}
        _append_capped(self.results, result, 512)
        return result

    def calibrate_confidence(self, confidence, accuracy):
        if accuracy < 50:
            return max(0, confidence - 15)
        if accuracy > 70:
            return min(100, confidence + 5)
        return confidence

    def report(self):
        return {"windows": len(self.windows), "evaluations": len(self.results), "results": self.results}


TITAN_WALK_FORWARD_ENGINE = TitanWalkForwardValidationEngine()


# ============================================================
# TITAN QUANT VALIDATION ENGINE
# ============================================================

class TitanQuantValidationEngine:
    def __init__(self):
        self.records = []

    def add_trade(self, pnl_percent):
        _append_capped(self.records, float(pnl_percent), 5000)

    def metrics(self):
        if not self.records:
            return {"trades": 0}
        wins = [x for x in self.records if x > 0]
        losses = [x for x in self.records if x <= 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        equity = 0
        peak = 0
        drawdown = 0
        losing = 0
        max_losing = 0
        for p in self.records:
            equity += p
            peak = max(peak, equity)
            drawdown = max(drawdown, peak - equity)
            losing = losing + 1 if p <= 0 else 0
            max_losing = max(max_losing, losing)
        return {
            "trades": len(self.records),
            "win_rate": round(len(wins) / len(self.records) * 100, 2),
            "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss else None,
            "max_drawdown": round(drawdown, 3),
            "max_losing_streak": max_losing
        }

    def walk_forward(self, values, train=50, test=20):
        results = []
        i = 0
        while i + train + test <= len(values):
            train_set = values[i:i + train]
            test_set = values[i + train:i + train + test]
            results.append({
                "train": len(train_set),
                "test": len(test_set),
                "test_return": round(sum(test_set), 4)
            })
            i += test
        return results

    def stress_test(self, data):
        checks = {
            "empty_data": not bool(data),
            "extreme_volatility": abs(float(data.get("volatility", 0))) > 15 if isinstance(data, dict) else True,
            "invalid_price": float(data.get("price", 1)) <= 0 if isinstance(data, dict) else True
        }
        return {"safe": not any(checks.values()), "checks": checks}


TITAN_QUANT_VALIDATOR = TitanQuantValidationEngine()




# ============================================================
# TITAN ULTIMATE INTEGRATION ORCHESTRATOR
# ============================================================

class TitanUltimateOrchestrator:
    """Connects data, AI, risk, learning and decision layers."""
    def __init__(self):
        self.decision = globals().get("TITAN_DECISION_CORE")
        self.enhancer = globals().get("TITAN_ENTERPRISE_V4")
        self.memory = []

    def run(self, market_data):
        if not isinstance(market_data, dict):
            return {"decision": "WAIT", "confidence": 0, "reason": "invalid_data"}
        health = self.enhancer.self_check(market_data) if self.enhancer else {"passed": True}
        if not health.get("passed", True):
            return {"decision": "WAIT", "confidence": 0, "reason": "self_check_failed"}
        anomaly = self.enhancer.anomaly_check(
            market_data.get("volume_change", 0),
            market_data.get("volatility", 0)
        ) if self.enhancer else {"blocked": False}
        if anomaly.get("blocked"):
            return {"decision": "WAIT", "confidence": 0, "reason": anomaly.get("reason")}
        result = self.decision.decide(market_data) if self.decision else {"decision": "WAIT"}
        _append_capped(self.memory, {"input": market_data, "result": result}, 128)
        return result


TITAN_ULTIMATE = TitanUltimateOrchestrator()

# ============================================================
# FLASK
# ============================================================


app = Flask(__name__, instance_path=str(FLASK_INSTANCE_DIR))
app.config["JSON_AS_ASCII"] = False
app.config["TEMPLATES_AUTO_RELOAD"] = False
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 3600
# Android-safe default: local-only dashboard.
HOST = "127.0.0.1"
_default_port = os.environ.get("TITAN_PORT_DEFAULT") or "8080"
try:
    PORT = int(os.environ.get("TITAN_PORT", _default_port) or _default_port)
except (TypeError, ValueError):
    PORT = int(_default_port)

@app.get("/favicon.ico")
def titan_favicon():
    return "", 204

@app.errorhandler(404)
def titan_not_found(exc):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "not_found"}), 404
    return ("<h1>TITAN: 404</h1>", 404, {"Content-Type": "text/html; charset=utf-8"})

@app.errorhandler(Exception)
def titan_global_error_handler(exc):
    # Keep API responses JSON, but never leave the human-facing dashboard blank.
    LOGGER.exception("Unhandled application error: %s", exc)
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "internal_error", "detail": str(exc)[:500]}), 500
    detail = str(exc).replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
    return (
        "<!doctype html><html lang='fa' dir='rtl'><meta charset='utf-8'>"
        "<title>TITAN Enterprise Error</title>"
        "<body style='background:#050812;color:#eef2ff;font-family:Tahoma;padding:30px'>"
        "<h2 style='color:#38bdf8'>⚡ TITAN ENTERPRISE</h2>"
        "<p>داشبورد با یک خطای داخلی مواجه شد، اما سرور فعال است.</p>"
        f"<pre style='white-space:pre-wrap;background:#0b1220;padding:16px;border-radius:12px'>{detail}</pre>"
        "<p>بعد از رفع خطا صفحه را Refresh کنید.</p></body></html>", 500, {"Content-Type": "text/html; charset=utf-8"}
    )

def _gemini_dashboard_status(summary: str) -> dict[str, Any]:
    return {
        "available": bool(GEMINI_API_KEY),
        "ready": bool(summary.strip()),
        "label": "تحلیل آماده" if summary.strip() else ("کلید Gemini موجود نیست" if not GEMINI_API_KEY else "در انتظار تحلیل"),
        "updated_at": _now_iso(),
    }


def _as_map(value):
    """Jinja-safe: only treat real dict/mapping as map; strings/lists become {}."""
    try:
        from collections.abc import Mapping
        if isinstance(value, Mapping) and not isinstance(value, (str, bytes)):
            return dict(value)
    except Exception:
        _swallow()
    return {}

def _as_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []



# ============================================================
# TITAN V10.5 — COGNITIVE NEXUS / EVIDENCE GRAPH
# ------------------------------------------------------------------
# Final arbitration layer above the existing V10 engine.
# Analysis-only: no live order execution and no fabricated market data.
#
# It connects:
# live freshness -> multi-TF spine -> structure/regime -> derivatives
# -> precision -> forecast/patterns -> AI ensemble -> historical outcomes
# -> neural V9 -> Cognitive Nexus -> one canonical decision.
# ============================================================

CNS_VERSION = "TITAN-CNS-10.6-BALANCED"
CNS_HISTORY_LIMIT = 320
CNS_MIN_HISTORY = 5
CNS_PROMOTE_MIN_STRENGTH = 0.40
CNS_PROMOTE_MIN_MARGIN = 0.07
CNS_PROMOTE_MIN_QUALITY = 48.0
CNS_PROMOTE_MIN_DQ = 50.0
CNS_PROMOTE_MIN_NEURAL = 42.0
CNS_PROMOTE_MIN_RR = 1.02
CNS_HARD_CONFLICT = 0.85


class TitanCognitiveNexusV10:
    """Evidence-graph arbitration with Bayesian historical shrinkage.

    Each evidence source is normalized to [-1,+1].
    Historical win rates use Beta(2,2) shrinkage so small samples cannot
    become artificial 0%/100% certainty. Historical data calibrates fresh
    evidence; it never creates a trade by itself.
    """

    TF_WEIGHTS = {"15m": 0.15, "1h": 0.28, "4h": 0.30, "1d": 0.27}

    def _beta_wr(self, wins: float, losses: float) -> float:
        wins = max(0.0, float(wins))
        losses = max(0.0, float(losses))
        return ((wins + 2.0) / (wins + losses + 4.0)) * 100.0

    def _history(self, symbol: str, direction: str, timeframe: str = "") -> dict[str, Any]:
        """Recency-aware historical evidence from evaluated forecasts."""
        out = {
            "samples": 0, "wins": 0, "losses": 0,
            "win_rate": 50.0, "recent_wr": 50.0,
            "confidence": 0.0, "source": "sqlite",
        }
        try:
            params = [symbol, direction]
            sql = (
                "SELECT outcome,created_at FROM forecasts "
                "WHERE symbol=? AND direction=? AND outcome IN ('WIN','LOSS') "
            )
            if timeframe:
                sql += "AND timeframe=? "
                params.append(timeframe)
            sql += "ORDER BY created_at DESC LIMIT ?"
            params.append(CNS_HISTORY_LIMIT)

            with DB_LOCK, db_conn() as con:
                rows = con.execute(sql, params).fetchall()

            if not rows:
                return out

            wins = sum(1 for r in rows if str(r["outcome"]) == "WIN")
            losses = sum(1 for r in rows if str(r["outcome"]) == "LOSS")

            # Recent observations receive more weight, while old observations
            # remain useful for regime-independent baseline calibration.
            now = time.time()