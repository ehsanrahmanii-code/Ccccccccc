            if decision_tag in {"LONG", "SHORT"} and meta_v8.get("label") == "REJECT":
                # V28.4: soft — keep direction, tag risk, lower quality
                fusion["meta_soft_reject"] = True
                fusion.setdefault("explanation", []).append("متا-برچسب V8 رد نرم → جهت حفظ با کاهش کیفیت")
            if decision_tag in {"LONG", "SHORT"}:
                register_portfolio_signal(symbol, decision_tag)
        except Exception as _mv:
            LOGGER.debug("meta_v8 failed: %s", _mv)

        # --- V9 Neural Synapse: full-system coherence gate ---
        try:
            path_stats = forecast_path_accuracy_stats(symbol)
            calib_v9 = calibrate_success_probability_v8(
                symbol=symbol,
                direction=decision_tag if decision_tag in {"LONG", "SHORT"} else ("LONG" if bias == "صعودی" else "SHORT" if bias == "نزولی" else ""),
                raw_prob=safe_float(fusion.get("success_probability"), 50),
                regime=str((regime or {}).get("regime", "")),
            )
            fusion["probability_calibration"] = calib_v9
            _prec_for_neural = precision_pre
            try:
                if "precision" in locals() and isinstance(precision, dict):
                    _prec_for_neural = precision
            except Exception:
                _swallow()
            _ai_for_neural = ai if isinstance(ai, dict) else {}
            _dq_for_neural = fusion.get("data_quality") or {}
            neural = TITAN_NEURAL.fuse(
                tf_scores=tf_scores,
                structure=structure,
                regime=regime,
                confluence=confluence,
                precision=_prec_for_neural,
                patterns=pattern_pack,
                forecast=candle_forecast,
                path_stats=path_stats,
                derivatives=derivatives,
                fusion=fusion,
                ai_opinions=_ai_for_neural,
                data_quality=_dq_for_neural,
                ladder=fusion.get("entry_ladder") or {},
                calib=calib_v9,
                quant_bias=bias,
                quant_score=float(score_int),
            )
            fusion["neural_v9"] = neural
            titan["fusion"]["neural_v9"] = neural
            titan["neural_score"] = neural.get("neural_score")
            titan["neural_confidence"] = neural.get("confidence")
            # Neural can only demote or confirm — never invent a side from WAIT quant without ladder
            n_side = neural.get("side", "WAIT")
            n_conf = safe_float(neural.get("confidence"), 0)
            if decision_tag in {"LONG", "SHORT"}:
                # Opportunity-preserving rule: a neutral neural layer is not enough by itself
                # to erase a strong, data-valid directional setup. Only demote when the
                # neural layer is materially uncertain AND the directional edge is weak.
                _dq_now = safe_float((fusion.get("data_quality") or {}).get("score"), 0)
                _sq_now = safe_float(signal_quality, 0)
                _edge_now = abs(safe_float(neural.get("neural_score"), 50) - 50)
                if n_side == "WAIT" and n_conf >= 70:
                    # Neutral neural never kills a data-valid directional edge by itself.
                    fusion.setdefault("explanation", []).append(
                        "سیناپس عصبی خنثی — جهت کمی حفظ شد (V28.5)"
                    )
                    signal_quality = min(int(signal_quality), 70)
                elif n_side != decision_tag and n_side in {"LONG", "SHORT"} and n_conf >= 82:
                    # Only extreme opposite neural with weak edge demotes to WAIT.
                    if _sq_now < 55 and _edge_now < 8:
                        decision_tag, bias = "WAIT", "خنثی"
                        fusion.setdefault("explanation", []).append(
                            f"سیناپس عصبی خلاف جهت قوی ({n_side}) conf={n_conf:.0f} + لبه ضعیف → انتظار"
                        )
                    else:
                        fusion.setdefault("explanation", []).append(
                            f"تعارض Neural ثبت شد اما Edge حفظ شد · {decision_tag}"
                        )
                        signal_quality = min(int(signal_quality), 68)
                elif n_side == decision_tag and n_conf >= 62:
                    fusion.setdefault("explanation", []).append(
                        f"سیناپس عصبی V9 تأیید {n_side} · score={neural.get('neural_score')} · conf={n_conf:.0f}"
                    )
            elif decision_tag == "WAIT" and n_side in {"LONG", "SHORT"} and n_conf >= 48:
                # Balanced opportunity promotion: allow a strong consensus setup to surface
                # even when one timeframe is merely early/neutral. Hard data failures and
                # strong opposite HTF/MTF structure still block the promotion.
                trial_ladder = entry_ladder_gate(tf_scores, tf_results, n_side, neural.get("bias", "خنثی"))
                _dq_promote = safe_float((fusion.get("data_quality") or {}).get("score"), 0)
                _rr_promote = _friction_adjusted_rr(price, sl, tp1)
                _neural_dir_ok = (safe_float(neural.get("neural_score"), 50) >= 55) if n_side == "LONG" else (safe_float(neural.get("neural_score"), 50) <= 45)
                _opportunity_ok = _dq_promote >= (MIN_DATA_QUALITY_SCORE - 2) and _neural_dir_ok
                _quality_ok = safe_float(signal_quality, 0) >= 52 and pscore >= 48 and success_prob >= 48 and _rr_promote >= 1.05
                _hard_opposite = (
                    (n_side == "LONG" and trial_ladder.get("htf_side") == "SHORT" and trial_ladder.get("htf_score",50) <= 43)
                    or (n_side == "SHORT" and trial_ladder.get("htf_side") == "LONG" and trial_ladder.get("htf_score",50) >= 57)
                )
                if _opportunity_ok and _quality_ok and not _hard_opposite:
                    decision_tag = n_side
                    bias = neural.get("bias", "صعودی" if n_side == "LONG" else "نزولی")
                    fusion["opportunity_mode"] = True
                    fusion.setdefault("explanation", []).append(
                        f"Opportunity Engine: {n_side} فعال شد · Neural={n_conf:.0f} · RR={_rr_promote:.2f} · تایم‌فریم‌ها در آستانه ورود"
                    )
            # Soft quality blend with neural confidence
            if isinstance(neural.get("neural_score"), (int, float)):
                signal_quality = int(round(clamp(
                    0.55 * signal_quality + 0.25 * safe_float(fusion.get("success_probability"), 50)
                    + 0.10 * pscore + 0.10 * n_conf,
                    0, 100,
                )))
        except Exception as _neu:
            LOGGER.debug("neural synapse V9 failed: %s", _neu)

        signal_quality = int(round(clamp(0.60 * signal_quality + 0.25 * success_prob + 0.15 * pscore, 0, 100)))
        if decision_tag == "WAIT":
            signal_quality = min(signal_quality, 68)
        effective_rr1 = _friction_adjusted_rr(price, sl, tp1)
        effective_rr2 = _friction_adjusted_rr(price, sl, tp2)
        if decision_tag in {"LONG", "SHORT"} and effective_rr1 < MIN_EFFECTIVE_RR:
            # V28.4: keep direction; only hard-kill if RR is tiny AND quality is weak.
            if effective_rr1 < 0.85 and (signal_quality < 50 or success_prob < 48):
                decision_tag, bias = "WAIT", "خنثی"
                signal_quality = min(signal_quality, 55)
                fusion["rr_veto"] = True
            else:
                fusion["rr_warning"] = True
                fusion["rr_warning_value"] = round(effective_rr1, 2)
                signal_quality = min(signal_quality, 72)
        # If AI majority conflicts hard, cap quality
        if (titan.get("fusion") or {}).get("ai_majority") in {"LONG", "SHORT"}:
            if decision_tag not in {"WAIT", (titan.get("fusion") or {}).get("ai_majority")} and ai_pre.get("providers", 0) >= 2:
                signal_quality = min(signal_quality, 60)

        meta_v8_label = str(((fusion.get("meta_v8") or {}).get("label") if isinstance(fusion, dict) else "") or meta.get("label") or "")
        neural_pack = (fusion.get("neural_v9") or {}) if isinstance(fusion, dict) else {}
        neural_side = str(neural_pack.get("side") or "")
        neural_conf = safe_float(neural_pack.get("confidence"), 0)
        neural_ok = (not neural_pack) or (neural_side in {decision_tag, "WAIT"} and neural_side != "WAIT") or (neural_side == decision_tag)
        if neural_pack and decision_tag in {"LONG", "SHORT"}:
            neural_ok = neural_side == decision_tag and neural_conf >= 55
        if (
            signal_quality >= 82
            and meta_v8_label == "ACCEPT"
            and decision_tag in {"LONG", "SHORT"}
            and success_prob >= 62
            and bool((fusion.get("entry_ladder") or {}).get("passed", False))
            and not fusion.get("dq_veto")
            and neural_ok
            and neural_conf >= 58
        ):
            signal_tag = "HIGH CONVICTION"
        elif signal_quality >= 70 and decision_tag in {"LONG", "SHORT"} and success_prob >= 56 and meta_v8_label != "REJECT" and (not neural_pack or neural_side in {decision_tag, "WAIT"}):
            signal_tag = "CONFIRMED SETUP"
        elif signal_quality >= 55:
            signal_tag = "WATCH"
        else:
            signal_tag = "LOW EDGE"

        signal_grade: dict[str, Any] = {"grade": "—", "label_fa": "—", "action": "—", "trust_index": 0, "checklist": [], "rank": 0}
        # --- V10 Professional Grade ---
        try:
            _pat_primary = (pattern_pack or {}).get("primary") or {}
            _pat_conf = safe_float(_pat_primary.get("confidence"), 0)
            _fc_bias = str((candle_forecast or {}).get("overall_bias") or "")
            _fc_aligned = (
                (decision_tag == "LONG" and _fc_bias == "صعودی")
                or (decision_tag == "SHORT" and _fc_bias == "نزولی")
            )
            signal_grade = classify_signal_grade(
                decision_tag=decision_tag,
                signal_quality=float(signal_quality),
                success_prob=float(success_prob),
                alignment=float(alignment),
                neural=neural_pack if isinstance(neural_pack, dict) else fusion.get("neural_v9"),
                meta=fusion.get("meta_v8") or meta,
                ladder=fusion.get("entry_ladder") or {},
                data_quality=fusion.get("data_quality") or {},
                precision=precision if isinstance(precision, dict) else precision_pre,
                effective_rr1=float(effective_rr1) if effective_rr1 is not None else 0.0,
                ai_agreement=safe_float((fusion.get("ai_agreement") if isinstance(fusion, dict) else None) or (ai_consensus or {}).get("agreement"), 0),
                pattern_conf=_pat_conf,
                forecast_aligned=_fc_aligned,
            )
            # V28.4: only hard F + data veto kills direction; D/C stay visible as risk-tagged signals
            if decision_tag in {"LONG", "SHORT"} and signal_grade.get("grade") == "F" and (fusion.get("data_quality") or {}).get("hard_veto"):
                decision_tag, bias = "WAIT", "خنثی"
                signal_tag = "LOW EDGE"
                titan["bias"] = bias
                fusion.setdefault("explanation", []).append(
                    f"درجه F + وتوی داده → سیگنال Cancel شد ({signal_grade.get('label_fa')})"
                )
            elif decision_tag in {"LONG", "SHORT"} and signal_grade.get("grade") in {"C", "D"}:
                signal_tag = "WATCH" if signal_grade.get("grade") == "C" else "LOW EDGE"
                fusion.setdefault("explanation", []).append(
                    f"درجه {signal_grade.get('grade')} → جهت حفظ شد با برچسب ریسک ({signal_grade.get('label_fa')})"
                )
            fusion["signal_grade"] = signal_grade
            titan["fusion"]["signal_grade"] = signal_grade
            titan["bias"] = bias
        except Exception as _gr:
            LOGGER.debug("signal grade failed: %s", _gr)
            signal_grade = {"grade": "—", "label_fa": "نامشخص", "action": "—", "trust_index": 0, "checklist": [], "rank": 0}

        # V27.3 FINAL BALANCED OPPORTUNITY PASS
        # Purpose: prevent a valid directional setup from being flattened to WAIT by
        # a single soft/secondary gate, while keeping hard data/safety vetoes intact.
        try:
            _f = fusion if isinstance(fusion, dict) else {}
            _dqf = safe_float((_f.get("data_quality") or {}).get("score"), 0)
            _neuf = (_f.get("neural_v9") or {}) if isinstance(_f, dict) else {}
            _nside = str(_neuf.get("side") or "")
            _nscore = safe_float(_neuf.get("neural_score"), 50)
            _nconf = safe_float(_neuf.get("confidence"), 0)
            _a_major = str(_f.get("ai_majority") or "")
            _cand = _nside if _nside in {"LONG","SHORT"} else _a_major if _a_major in {"LONG","SHORT"} else (
                "LONG" if bias == "صعودی" else "SHORT" if bias == "نزولی" else (
                    "LONG" if score_int >= 55 else "SHORT" if score_int <= 45 else ""
                )
            )
            _tfvals = [safe_float(v,50) for v in (tf_scores or {}).values()]
            _tfavg = float(np.mean(_tfvals)) if _tfvals else 50.0
            _dir_edge = abs(float(score_int)-50.0)
            _rr_ok = safe_float(effective_rr1, 0) >= MIN_EFFECTIVE_RR
            _meta_final = str((_f.get("meta_v8") or {}).get("label") or "")
            _hard_block = bool(
                _f.get("dq_veto")
                or _f.get("portfolio_veto")
                or (_meta_final == "REJECT" and _dqf < 48)
                or (_f.get("ai_conflict") and _nconf >= 82)
            )
            _grade_now = str((signal_grade or {}).get("grade") or "")
            _strong_dir = (
                _cand in {"LONG","SHORT"}
                and _dqf >= 46
                and _dir_edge >= 3.5
                and safe_float(alignment,0) >= 44
                and safe_float(pscore,0) >= 40
                and safe_float(success_prob,0) >= 44
                and _rr_ok
                and not _hard_block
                and _grade_now not in {"F"}
            )
            _support_votes = 0
            _support_reasons = []
            _struct_final = _f.get("structure") or {}
            if _cand == "LONG":
                if _nside == "LONG" and _nconf >= 52:
                    _support_votes += 1; _support_reasons.append("neural")
                if _a_major == "LONG":
                    _support_votes += 1; _support_reasons.append("ai")
                if score_int >= 54:
                    _support_votes += 1; _support_reasons.append("score")
                if _tfavg >= 53:
                    _support_votes += 1; _support_reasons.append("tf")
                if str(_struct_final.get("bias") or "") == "صعودی":
                    _support_votes += 1; _support_reasons.append("structure")
            elif _cand == "SHORT":
                if _nside == "SHORT" and _nconf >= 52:
                    _support_votes += 1; _support_reasons.append("neural")
                if _a_major == "SHORT":
                    _support_votes += 1; _support_reasons.append("ai")
                if score_int <= 46:
                    _support_votes += 1; _support_reasons.append("score")
                if _tfavg <= 47:
                    _support_votes += 1; _support_reasons.append("tf")
                if str(_struct_final.get("bias") or "") == "نزولی":
                    _support_votes += 1; _support_reasons.append("structure")
            _candidate_support = _support_votes >= 2
            if decision_tag == "WAIT" and _strong_dir and _candidate_support:
                _hard_htf_opposite = (
                    (_cand == "LONG" and safe_float((_f.get("entry_ladder") or {}).get("htf_score"),50) <= 34)
                    or (_cand == "SHORT" and safe_float((_f.get("entry_ladder") or {}).get("htf_score"),50) >= 66)
                )
                if not _hard_htf_opposite:
                    decision_tag = _cand
                    bias = "صعودی" if _cand == "LONG" else "نزولی"
                    _f["opportunity_mode"] = True
                    _f.setdefault("explanation", []).append(
                        f"Opportunity-Preserve V28.2: {_cand} فعال شد؛ لبه جهت‌دار واقعی، RR کافی، بدون وتوی سخت و {_support_votes} شاهد مستقل ({', '.join(_support_reasons[:4])})."
                    )
                    signal_quality = max(signal_quality, min(82, int(round(52 + _dir_edge * 1.6))))
            # WAIT only when no independent directional evidence exists.
        except Exception as _opp_final_exc:
            LOGGER.debug("final opportunity pass failed: %s", _opp_final_exc)

        edge = {
            "confluence": confluence, "regime": regime, "liquidity": liquidity, "structure": structure,
            "ai": ai_consensus, "meta": meta, "risk": risk, "governor": governor,
            "strategy_lab": {"status": "DEFERRED", "reason": "V51 hot path excludes future-return diagnostic lab"},
            "decision_tag": decision_tag,
            "fusion": titan.get("fusion") or {},
            "precision": precision,
        }

        base_symbol = symbol.split("/")[0].upper()
        coin_name, coin_icon = COIN_META.get(base_symbol, (base_symbol, "●"))
        color = "#4ade80" if score_int >= 60 else "#f43f5e" if score_int <= 40 else "#fbbf24"
        return {
            "symbol": symbol, "base_symbol": base_symbol, "coin_name": coin_name, "coin_icon": coin_icon,
            "tv_symbol": _binance_symbol(symbol), "price": smart_format(price), "rsi": f"{rsi:.1f}",
            "entry_valid": smart_format(price), "tp1": smart_format(tp1), "tp2": smart_format(tp2), "stop_loss": smart_format(sl),
            "coinglass_oi": derivatives["oi"], "coinglass_funding": derivatives["funding"], "derivatives_source": derivatives["source"],
            "btc_trend": btc_trend, "tfs": tf_results, "titan_analysis": titan, "ai_opinions": ai,
            "alignment": alignment, "bias": bias, "score": score_int, "score_bar": score_int, "score_color": color,
            "score_icon": "🟢" if score_int >= 60 else "🔴" if score_int <= 40 else "🟡", "volume_spike": volume_spike,
            "provisional_tf_score": round(provisional, 1), "volatility_pct": round(volatility_pct, 3),
            "risk_distance_pct": round(risk_distance_pct, 3), "rr_tp1": round(rr_tp1, 2), "rr_tp2": round(rr_tp2, 2),
            "effective_rr_tp1": round(effective_rr1, 2), "effective_rr_tp2": round(effective_rr2, 2),
            "precision": precision, "entry_timing": precision.get("entry_timing", "CAUTIOUS"),
            "signal_quality": signal_quality, "signal_tag": signal_tag,
            "fusion": titan.get("fusion") or {}, "edge": edge, "decision_tag": decision_tag,
            "success_probability": safe_float((titan.get("fusion") or {}).get("success_probability"), 50),
            "price_source": price_source,
            "live_price_age_sec": round(live_age_sec, 3) if live_age_sec is not None else None,
            "live_price_timestamp": live_timestamp,
            "closed_1h_candle_close_ms": int(df1h["T"].iloc[-1]),
            "closed_15m_candle_close_ms": int(df15["T"].iloc[-1]),
            "analysis_asof_utc": datetime.fromtimestamp(live_timestamp or last_closed_1h_ts, timezone.utc).isoformat(timespec="seconds"),
            "candle_state": "CLOSED_ONLY_INDICATORS",
            "ai_majority": (titan.get("fusion") or {}).get("ai_majority"),
            "ai_conflict": bool((titan.get("fusion") or {}).get("ai_conflict")),
            "decision_terminal": TITAN_EDGE_SUITE.decision_terminal({
                "symbol": symbol, "price": smart_format(price), "bias": bias,
                "rr_tp1": round(rr_tp1, 2), "rr_tp2": round(rr_tp2, 2), "effective_rr_tp1": round(effective_rr1, 2),
                "precision": precision, "signal_quality": signal_quality,
                "signal_tag": signal_tag, "edge": edge, "decision_tag": decision_tag}),
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "macd": macd_info,
            "stoch_rsi": stoch_info,
            "pivots": {k: smart_format(v) for k, v in (pivots or {}).items()},
            "volume_delta": vol_delta,
            "session": session_info,
            "long_short_ratio": derivatives.get("long_short_ratio"),
            "long_ratio": derivatives.get("long_ratio"),
            "short_ratio": derivatives.get("short_ratio"),
            "top_long_ratio": derivatives.get("top_long_ratio"),
            "taker_buy_pct": derivatives.get("taker_buy_pct"),
            "taker_sell_pct": derivatives.get("taker_sell_pct"),
            "adv_nudge": round(adv_nudge, 2),
            "patterns": pattern_pack,
            "candle_forecast": candle_forecast,
            "rsi_value": round(rsi, 2),
            "macd_hist": macd_info.get("hist"),
            "macd_cross": macd_info.get("cross"),
            "btc_correlation": (fusion.get("btc_correlation") if isinstance(fusion, dict) else None) or {},
            "order_blocks_fvg": structure_zones,
            "depth": depth_snap,
            "technical": {
                "fibonacci": {k: smart_format(v) for k, v in fib.items()},
                "support": smart_format(sr.get("support")) if sr.get("support") is not None else "N/A",
                "resistance": smart_format(sr.get("resistance")) if sr.get("resistance") is not None else "N/A",
                "bollinger": {
                    "upper": smart_format(bb.get("upper")) if bb.get("upper") is not None else "N/A",
                    "middle": smart_format(bb.get("middle")) if bb.get("middle") is not None else "N/A",
                    "lower": smart_format(bb.get("lower")) if bb.get("lower") is not None else "N/A",
                },
            },
            "tf_scores": {k: round(float(v), 1) for k, v in tf_scores.items()},
            "buy_sell": (lambda _tb, _ts, _vd, _press, _rsi, _sc: (lambda _bp: {
                "pressure": round(float(_press), 4),
                "bias": ("buy" if _bp >= 55 else "sell" if _bp <= 45 else "neutral"),
                "state": ("buy_pressure" if _bp >= 58 else "sell_pressure" if _bp <= 42 else "balanced"),
                "taker_buy_pct": _tb if _tb is not None else None,
                "taker_sell_pct": _ts if _ts is not None else None,
                "taker_available": _tb is not None,
                "volume_delta_pct": round(float(_vd), 2),
                "volume_delta_is_proxy": True,
                "buy_pct": int(round(_bp)),
                "sell_pct": int(round(100 - _bp)),
                "source": (
                    "Binance-Taker" if _tb is not None
                    else ("volume_body_proxy" if abs(float(_vd)) > 0.01 else "rsi_score_proxy")
                ),
            })(float(clamp(
                (float(_tb) if _tb is not None else (
                    50.0
                    + float(np.clip(_vd, -40, 40)) * 0.55
                    + (float(_rsi) - 50.0) * 0.25
                    + (float(_sc) - 50.0) * 0.20
                )),
                5, 95
            ))))(
                derivatives.get("taker_buy_pct"),
                derivatives.get("taker_sell_pct"),
                safe_float(vol_delta.get("delta_pct"), 0) if isinstance(vol_delta, dict) else 0,
                safe_float((edge.get("liquidity") or {}).get("pressure"), 0) if isinstance(edge, dict) else 0,
                safe_float(rsi, 50),
                safe_float(score_int, 50),
            ),
            "whale": (lambda _od, _src, _fund, _tb: {
                "oi": derivatives.get("oi", "N/A"),
                "oi_delta": round(_od, 3) if math.isfinite(_od) else 0.0,
                "funding": derivatives.get("funding", "N/A"),
                "funding_value": _fund if math.isfinite(_fund) else None,
                "taker_buy_pct": _tb,
                "note": titan.get("oi_note", "") or titan.get("funding_note", ""),
                "source": _src or "N/A",
                "is_proxy": not (isinstance(_src, str) and ("CoinGlass" in _src or "Binance" in _src)),
                "label": (
                    "Whale inflow / OI rising" if math.isfinite(_od) and _od > 1.5 and isinstance(_src, str) and ("CoinGlass" in _src or "Binance" in _src)
                    else "Liquidity exit / OI falling" if math.isfinite(_od) and _od < -1.5 and isinstance(_src, str) and ("CoinGlass" in _src or "Binance" in _src)
                    else (
                        "Taker buy dominant" if _tb is not None and _tb >= 58
                        else "Taker sell dominant" if _tb is not None and _tb <= 42
                        else "Neutral / limited OI evidence"
                    )
                ),
                "score": int(round(clamp(
                    50
                    + (float(np.clip(_od, -10, 10)) * 3 if math.isfinite(_od) else 0)
                    + ((float(_tb) - 50) * 0.4 if _tb is not None else 0)
                    + ((float(_fund) * 80) if math.isfinite(_fund) else 0),
                    0, 100
                ))),
            })(
                safe_float(derivatives.get("oi_delta"), float("nan")),
                derivatives.get("source"),
                safe_float(derivatives.get("funding_value"), float("nan")),
                derivatives.get("taker_buy_pct"),
            ),
            "sentiment": {
                "rsi": round(rsi, 1),
                "rsi_note": titan.get("rsi_note", ""),
                "funding_note": titan.get("funding_note", ""),
                "score": score_int,
                "label": "مثبت" if score_int >= 60 else "منفی" if score_int <= 40 else "خنثی",
            },
            "data_quality": fusion.get("data_quality") if isinstance(fusion, dict) else {},
            "entry_ladder": fusion.get("entry_ladder") if isinstance(fusion, dict) else {},
            "meta_v8": fusion.get("meta_v8") if isinstance(fusion, dict) else {},
            "neural_v9": fusion.get("neural_v9") if isinstance(fusion, dict) else {},
            "neural_score": (fusion.get("neural_v9") or {}).get("neural_score") if isinstance(fusion, dict) else None,
            "neural_confidence": (fusion.get("neural_v9") or {}).get("confidence") if isinstance(fusion, dict) else None,
            "signal_grade": signal_grade if isinstance(signal_grade, dict) else (fusion.get("signal_grade") if isinstance(fusion, dict) else {}),
            "grade": (signal_grade.get("grade") if isinstance(signal_grade, dict) else None) or "—",
            "grade_label": (signal_grade.get("label_fa") if isinstance(signal_grade, dict) else None) or "—",
            "grade_action": (signal_grade.get("action") if isinstance(signal_grade, dict) else None) or "—",
            "trust_index": (signal_grade.get("trust_index") if isinstance(signal_grade, dict) else None) or 0,
            "grade_checklist": (signal_grade.get("checklist") if isinstance(signal_grade, dict) else None) or [],
            "forecast_accuracy": fusion.get("forecast_accuracy") if isinstance(fusion, dict) else {},
            "param_version": TITAN_PARAM_VERSION,
            "confidence_gates": {
                "dq_veto": bool(isinstance(fusion, dict) and fusion.get("dq_veto")),
                "ladder_veto": bool(isinstance(fusion, dict) and fusion.get("ladder_veto")),
                "forecast_veto": bool(isinstance(fusion, dict) and fusion.get("forecast_veto")),
                "portfolio_veto": bool(isinstance(fusion, dict) and fusion.get("portfolio_veto")),
                "precision_veto": bool(isinstance(fusion, dict) and fusion.get("precision_veto")),
                "neural_veto": bool(isinstance(fusion, dict) and (fusion.get("neural_v9") or {}).get("side") == "WAIT" and decision_tag in {"LONG", "SHORT"}),
            },
        }
    except Exception as exc:
        LOGGER.exception("analyze_asset failed for %s: %s", symbol, exc)
        return None


# ============================================================
# TITAN PROFESSIONAL EDGE SUITE - 12 ADDITIVE MODULES
# No external dependency. Every module is fail-safe and analysis-only.
# ============================================================

class TitanProfessionalEdgeSuite:
    """Twelve professional research layers wired into the existing TITAN pipeline.

    1) Signal Confluence Engine
    2) Regime Detection Engine
    3) Liquidity / Order-Flow Proxy
    4) Market Structure Engine
    5) Walk-Forward / OOS validator
    6) Strategy Laboratory
    7) Meta-Labeling
    8) Adaptive Risk Engine
    9) Drawdown Governor
    10) AI Ensemble Consensus
    11) Counterfactual Engine
    12) Decision Terminal payload builder

    The engine is deliberately dependency-free and conservative: missing market/API data
    lowers confidence instead of inventing values. It never executes real orders.
    """

    def __init__(self):
        self.name = "TITAN PROFESSIONAL EDGE SUITE"
        self.memory_path = MEMORY_DIR / "professional_edge_memory.json"
        self.memory = self._load_memory()

    def _load_memory(self):
        try:
            data = _load_json(self.memory_path, {})
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_memory(self):
        try:
            _save_json(self.memory_path, self.memory)
        except Exception as exc:
            LOGGER.debug("Professional edge memory save skipped: %s", exc)

    # 1) Signal Confluence Engine
    def signal_confluence(self, *, score=50, alignment=50, rsi=50, price=0, vwap=0,
                          ema20=0, ema50=0, volume_spike=False, derivatives=None,
                          structure=None, regime="unknown"):
        derivatives = derivatives or {}
        structure = structure or {}
        checks = []
        points = []
        if score >= 60:
            checks.append(("momentum", 1)); points.append("مومنتوم صعودی")
        elif score <= 40:
            checks.append(("momentum", 1)); points.append("مومنتوم نزولی")
        else:
            checks.append(("momentum", 0))
        checks.append(("mtf", clamp(alignment / 100.0, 0, 1)))
        if price and vwap:
            checks.append(("vwap", 1 if (score >= 60 and price >= vwap) or (score <= 40 and price <= vwap) else 0.35))
        if ema20 and ema50:
            ema_ok = 1 if (score >= 60 and ema20 >= ema50) or (score <= 40 and ema20 <= ema50) else 0.25
            checks.append(("trend", ema_ok))
        checks.append(("volume", 1.0 if volume_spike else 0.4))
        oi = safe_float(derivatives.get("oi_delta"), 0)
        funding = safe_float(derivatives.get("funding_value"), 0)
        oi_score = 0.55
        if score >= 60 and oi > 0: oi_score = 0.9
        elif score <= 40 and oi > 0: oi_score = 0.8
        elif oi < 0: oi_score = 0.4
        checks.append(("open_interest", oi_score))
        structure_score = safe_float(structure.get("confirmation_score"), 50) / 100.0
        checks.append(("structure", clamp(structure_score, 0, 1)))
        regime_bonus = 1.0 if regime not in {"range", "unknown"} else 0.65
        checks.append(("regime", regime_bonus))
        raw = sum(v for _, v in checks) / max(1, len(checks))
        confluence = int(round(clamp(35 + raw * 65, 0, 100)))
        disagreement = [name for name, val in checks if val < 0.5]
        return {"score": confluence, "components": {k: round(float(v), 3) for k, v in checks},
                "disagreements": disagreement, "label": "STRONG CONFLUENCE" if confluence >= 80 else "CONFIRMED" if confluence >= 68 else "WATCH"}

    # 2) Regime Detection Engine
    def regime_detection(self, df, atr=None, score=50):
        try:
            close = df["close"].astype(float)
            if len(close) < 60: return {"regime": "unknown", "confidence": 0, "features": {}}
            ret20 = float(close.iloc[-1] / close.iloc[-21] - 1) if len(close) > 21 else 0.0
            ret5 = float(close.iloc[-1] / close.iloc[-6] - 1) if len(close) > 6 else 0.0
            atr_pct = float(atr / close.iloc[-1] * 100) if atr is not None and close.iloc[-1] else 0.0
            ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
            ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
            slope = abs(ret20) * 100
            if atr_pct >= 4.5:
                regime = "high_volatility"
            elif ret20 > 0.05 and ema20 > ema50 and slope > 1.5:
                regime = "trend_up"
            elif ret20 < -0.05 and ema20 < ema50 and slope > 1.5:
                regime = "trend_down"
            elif abs(ret5) > 0.02 and abs(ret20) < 0.04:
                regime = "breakout_watch"
            elif atr_pct < 1.2 and abs(ret20) < 0.025:
                regime = "range"
            else:
                regime = "transition"
            score_fit = 100 - min(80, abs((score - 50) - (20 if "up" in regime else -20 if "down" in regime else 0)) * 1.8)
            return {"regime": regime, "confidence": int(round(clamp(score_fit, 20, 95))),
                    "features": {"ret5_pct": round(ret5*100,3), "ret20_pct": round(ret20*100,3), "atr_pct": round(atr_pct,3), "ema_spread_pct": round((ema20/ema50-1)*100,3) if ema50 else 0}}
        except Exception as exc:
            LOGGER.debug("Regime detection failed: %s", exc)
            return {"regime": "unknown", "confidence": 0, "features": {}}

    # 3) Liquidity / Order-Flow Proxy
    def liquidity_map(self, df, current_price):
        try:
            data = df.tail(60).copy()
            price = float(current_price)
            if data.empty or price <= 0: raise ValueError("invalid liquidity input")
            vol = pd.to_numeric(data["vol"], errors="coerce").fillna(0)
            high_vol = data.loc[vol.nlargest(min(8, len(vol))).index]
            upper = float(high_vol["high"].median()) if not high_vol.empty else price
            lower = float(high_vol["low"].median()) if not high_vol.empty else price
            ranges = {"upper_pool": round(upper, 8), "lower_pool": round(lower, 8),
                      "upper_distance_pct": round((upper/price-1)*100,3),
                      "lower_distance_pct": round((lower/price-1)*100,3)}
            if upper > price * 1.003 and lower < price * 0.997:
                state = "balanced_liquidity"
            elif upper > price * 1.01:
                state = "overhead_liquidity"
            elif lower < price * 0.99:
                state = "downside_liquidity"
            else:
                state = "compressed"
            # buy/sell pressure proxy from candle location + volume
            clv = ((data["close"] - data["low"]) - (data["high"] - data["close"])) / (data["high"] - data["low"]).replace(0, np.nan)
            pressure = float((clv.fillna(0) * vol).sum() / max(float(vol.sum()), 1.0))
            return {"state": state, "pressure": round(pressure, 3), "map": ranges,
                    "liquidity_bias": "buying" if pressure > 0.12 else "selling" if pressure < -0.12 else "balanced"}
        except Exception as exc:
            LOGGER.debug("Liquidity map failed: %s", exc)
            return {"state": "unavailable", "pressure": 0, "map": {}, "liquidity_bias": "unknown"}

    # 4) Market Structure Engine
    def market_structure(self, df):
        """Causal market structure using only the current bar and prior bars.

        No centered windows are used: BOS compares the latest CLOSED close with
        prior closed-bar extremes, so the feature is safe for replay/backtest.
        """
        try:
            d = df.tail(80).copy()
            if d.empty or len(d) < 12:
                return {"event": "UNKNOWN", "confirmation_score": 0, "bias": "خنثی"}
            prior_high = d["high"].shift(1).rolling(20, min_periods=5).max()
            prior_low = d["low"].shift(1).rolling(20, min_periods=5).min()
            breakout_highs = d.loc[(d["high"] >= d["high"].shift(1).rolling(5, min_periods=5).max()) & prior_high.notna(), "high"].tail(6).tolist()
            breakout_lows = d.loc[(d["low"] <= d["low"].shift(1).rolling(5, min_periods=5).min()) & prior_low.notna(), "low"].tail(6).tolist()
            last = float(d["close"].iloc[-1])
            ref_high = float(prior_high.iloc[-1]) if np.isfinite(prior_high.iloc[-1]) else float(d["high"].iloc[:-1].tail(20).max())
            ref_low = float(prior_low.iloc[-1]) if np.isfinite(prior_low.iloc[-1]) else float(d["low"].iloc[:-1].tail(20).min())
            recent_high = max(breakout_highs[-3:]) if breakout_highs else ref_high
            recent_low = min(breakout_lows[-3:]) if breakout_lows else ref_low
            prev_high = breakout_highs[-4] if len(breakout_highs) >= 4 else recent_high
            prev_low = breakout_lows[-4] if len(breakout_lows) >= 4 else recent_low
            higher_high = recent_high > prev_high * 1.0005
            higher_low = recent_low > prev_low * 1.0005
            lower_high = recent_high < prev_high * 0.9995
            lower_low = recent_low < prev_low * 0.9995
            if last > ref_high * 1.001:
                event = "BOS_UP"
            elif last < ref_low * 0.999:
                event = "BOS_DOWN"
            elif higher_high and higher_low:
                event = "BULL_STRUCTURE"
            elif lower_high and lower_low:
                event = "BEAR_STRUCTURE"
            else:
                event = "RANGE_STRUCTURE"
            confirmation = 80 if event in {"BOS_UP", "BOS_DOWN"} else 72 if event in {"BULL_STRUCTURE", "BEAR_STRUCTURE"} else 48
            return {
                "event": event, "last": round(last, 8),
                "swing_high": round(recent_high, 8), "swing_low": round(recent_low, 8),
                "reference_high": round(ref_high, 8), "reference_low": round(ref_low, 8),
                "confirmation_score": confirmation,
                "bias": "صعودی" if event in {"BOS_UP", "BULL_STRUCTURE"} else "نزولی" if event in {"BOS_DOWN", "BEAR_STRUCTURE"} else "خنثی"
            }
        except Exception as exc:
            LOGGER.debug("Market structure failed: %s", exc)
            return {"event": "UNKNOWN", "confirmation_score": 0, "bias": "خنثی"}

    # 5) Walk-forward / OOS validation helper
    def out_of_sample_check(self, returns, min_samples=30, return_unit="pct"):
        arr = np.asarray(returns, dtype=float)
        arr = arr[np.isfinite(arr)]
        if arr.size < min_samples:
            return {"samples": int(arr.size), "oos_expectancy": 0.0, "oos_win_rate": 0.0,
                    "train_expectancy": 0.0, "status": "insufficient", "min_samples": int(min_samples),
                    "return_unit": return_unit}
        split = min(max(int(arr.size * 0.7), 10), arr.size - 10)
        train, test = arr[:split], arr[split:]
        decisive = test[test != 0]
        wins = int((decisive > 0).sum()); losses = int((decisive < 0).sum())
        return {"samples": int(test.size), "oos_expectancy": round(float(test.mean()), 4),
                "oos_win_rate": round(wins / (wins + losses) * 100, 2) if wins + losses else 0.0,
                "train_expectancy": round(float(train.mean()), 4), "status": "ok",
                "min_samples": int(min_samples), "return_unit": return_unit}

    # 6) Strategy Laboratory
    def strategy_lab(self, df, horizon=3, min_signals=30):
        strategies = {}
        try:
            close = pd.to_numeric(df["close"], errors="coerce")
            ema20 = close.ewm(span=20, adjust=False).mean(); ema50 = close.ewm(span=50, adjust=False).mean()
            rsi = wilder_rsi(close)
            signal_map = {
                "trend": np.where(ema20 > ema50, 1, np.where(ema20 < ema50, -1, 0)),
                "momentum": np.where(rsi > 55, 1, np.where(rsi < 45, -1, 0)),
                "breakout": np.where(close > close.rolling(20).max().shift(1), 1, np.where(close < close.rolling(20).min().shift(1), -1, 0)),
                "mean_reversion": np.where(rsi < 30, 1, np.where(rsi > 70, -1, 0)),
            }
            for name, signal in signal_map.items():
                sig = pd.Series(signal, index=df.index).fillna(0).astype(float)
                future = close.shift(-horizon) / close - 1.0
                raw = (future * sig * 100.0) - (TOTAL_ENTRY_BUFFER * 2.0 * 100.0)
                valid = raw[(sig != 0) & future.notna()].replace([np.inf, -np.inf], np.nan).dropna()
                metrics = _safe_return_series(valid.tolist(), return_unit="pct")
                strategies[name] = {
                    "signals": int((sig != 0).sum()), "complete_signals": int(valid.size),
                    "win_rate": metrics["win_rate"], "loss_rate": metrics["loss_rate"],
                    "expectancy_pct": metrics["expectancy"], "profit_factor": metrics["profit_factor"],
                    "max_drawdown_pct": metrics["max_drawdown"], "max_losing_streak": metrics["max_losing_streak"],
                }
            eligible = [(n,v) for n,v in strategies.items() if v["complete_signals"] >= min_signals]
            leader = max(eligible, key=lambda kv: kv[1]["expectancy_pct"], default=(None, {}))
            return {"strategies": strategies, "leader": leader[0], "leader_expectancy_pct": leader[1].get("expectancy_pct",0),
                    "horizon_bars": int(horizon), "min_signals": int(min_signals), "cost_model": "round_trip_fee+spread+slippage"}
        except Exception as exc:
            LOGGER.debug("Strategy lab failed: %s", exc)
            return {"strategies": {}, "leader": None, "leader_expectancy_pct": 0, "horizon_bars": int(horizon), "min_signals": int(min_signals)}

    # 7) Meta-Labeling
    def meta_label(self, confluence, quality, historical_win_rate=0):
        base = 0.45 * confluence + 0.35 * quality + 0.20 * clamp(historical_win_rate, 0, 100)
        probability = clamp(base, 0, 100)
        label = "ACCEPT" if probability >= 72 else "WATCH" if probability >= 58 else "REJECT"
        return {"probability": round(probability,2), "label": label}

    # 8) Adaptive Risk Engine
    def adaptive_risk(self, base_risk=1.0, volatility_pct=1.0, confidence=50, regime="unknown", drawdown_pct=0):
        factor = 1.0
        if volatility_pct > 4: factor *= 0.55
        elif volatility_pct > 2.5: factor *= 0.75
        elif volatility_pct < 0.8: factor *= 1.05
        if confidence < 60: factor *= 0.75
        elif confidence > 80: factor *= 1.05
        if regime in {"high_volatility", "transition"}: factor *= 0.75
        if drawdown_pct > 10: factor *= 0.45
        elif drawdown_pct > 5: factor *= 0.65
        return {"base_risk_pct": round(base_risk,3), "risk_pct": round(clamp(base_risk*factor,0.1,2.5),3), "factor": round(factor,3)}

    # 9) Drawdown Governor
    def drawdown_governor(self, values, unit="r"):
        arr = np.asarray(values, dtype=float); arr = arr[np.isfinite(arr)]
        if arr.size == 0:
            return {"state": "NORMAL", "drawdown_pct": 0.0, "risk_multiplier": 1.0, "unit": unit, "observations": 0}
        account_returns = arr / 100.0 if unit == "pct" else arr * 0.01
        account_returns = np.clip(account_returns, -0.999, 10.0)
        equity = np.cumprod(1.0 + account_returns)
        peak = np.maximum.accumulate(equity)
        dd_pct = float(np.max((peak - equity) / np.maximum(peak, 1e-12)) * 100.0)
        if dd_pct >= 15: state, mult = "DEFENSIVE", 0.35
        elif dd_pct >= 10: state, mult = "REDUCED", 0.55
        elif dd_pct >= 5: state, mult = "CAUTIOUS", 0.75
        else: state, mult = "NORMAL", 1.0
        return {"state": state, "drawdown_pct": round(dd_pct,3), "risk_multiplier": mult, "unit": unit, "observations": int(arr.size)}

    # 10) AI Ensemble Consensus
    def ai_ensemble(self, ai_opinions, titan_bias):
        mapping = {"صعودی": "LONG", "نزولی": "SHORT", "خنثی": "WAIT"}
        target = mapping.get(titan_bias, "WAIT")
        votes = []; providers = []; details = {}
        import re
        final_re = re.compile(r"نتیجه\s*نهایی\s*[:：-]?\s*(صعودی|نزولی|انتظار|خنثی)", re.I)
        for provider, raw_text in (ai_opinions or {}).items():
            if provider in {"ai_status", "providers", "internal", "titan"} or not isinstance(raw_text, str):
                continue
            text = raw_text.strip(); t = text.lower()
            if not text:
                continue
            # Prefer the explicitly requested final label. Keyword fallback only uses the
            # tail of the answer, reducing false votes when both bull/bear risks are discussed.
            matches = final_re.findall(text)
            if matches:
                lab = matches[-1]
                vote = "LONG" if lab == "صعودی" else "SHORT" if lab == "نزولی" else "WAIT"
            else:
                tail = t[-320:]
                long_hits = sum(tail.count(k) for k in ("صعودی", "bull", "long", "خرید"))
                short_hits = sum(tail.count(k) for k in ("نزولی", "bear", "short", "فروش"))
                vote = "LONG" if long_hits > short_hits else "SHORT" if short_hits > long_hits else "WAIT"
            votes.append(vote); providers.append(provider)
            details[provider] = {"vote": vote, "snippet": (text[:180] + "…") if len(text) > 180 else text}
        total = len(votes)
        agree = sum(v == target for v in votes)
        long_n = votes.count("LONG"); short_n = votes.count("SHORT"); wait_n = votes.count("WAIT")
        majority = "LONG" if long_n > short_n and long_n > wait_n else "SHORT" if short_n > long_n and short_n > wait_n else "WAIT"
        majority_n = max(long_n, short_n, wait_n) if total else 0
        return {
            "target": target, "votes": dict(zip(providers, votes)), "details": details,
            "agreement": round(agree / total * 100, 1) if total else 0.0,
            "majority_agreement": round(majority_n / total * 100, 1) if total else 0.0,
            "providers": total, "tally": {"LONG": long_n, "SHORT": short_n, "WAIT": wait_n},
            "majority": majority,
            "status": "CONSENSUS" if total and majority_n / total >= 0.66 else "MIXED" if total else "NO_AI_DATA",
        }

    # 11) Counterfactual Engine
    def counterfactual(self, price, sl, tp1, tp2, direction, future_prices):
        result = {"path": "NONE", "bars_to_tp1": None, "bars_to_tp2": None, "bars_to_sl": None, "mae_pct": 0.0, "mfe_pct": 0.0}
        try:
            future = [float(x) for x in future_prices if float(x) > 0]
            if not future or price <= 0: return result
            signed = [((x-price)/price*100) * (1 if direction != "نزولی" else -1) for x in future]
            result["mfe_pct"] = round(max(signed), 3); result["mae_pct"] = round(min(signed), 3)
            tp1_hit = (tp1 > price and any(x >= tp1 for x in future)) if direction != "نزولی" else any(x <= tp1 for x in future)
            tp2_hit = (tp2 > price and any(x >= tp2 for x in future)) if direction != "نزولی" else any(x <= tp2 for x in future)
            sl_hit = any(x <= sl for x in future) if direction != "نزولی" else any(x >= sl for x in future)
            if tp2_hit: result["path"] = "TP2"
            elif tp1_hit: result["path"] = "TP1"
            elif sl_hit: result["path"] = "SL"
            if tp1_hit:
                result["bars_to_tp1"] = next(i+1 for i,x in enumerate(future) if (x >= tp1 if direction != "نزولی" else x <= tp1))
            if tp2_hit:
                result["bars_to_tp2"] = next(i+1 for i,x in enumerate(future) if (x >= tp2 if direction != "نزولی" else x <= tp2))
            if sl_hit:
                result["bars_to_sl"] = next(i+1 for i,x in enumerate(future) if (x <= sl if direction != "نزولی" else x >= sl))
            return result
        except Exception:
            return result

    # 12) Decision Terminal
    def decision_terminal(self, item):
        edge = item.get("edge") or {}
        return {
            "symbol": item.get("symbol"), "price": item.get("price"), "bias": item.get("bias"),
            "decision_tag": item.get("decision_tag") or edge.get("decision_tag") or "WAIT",
            "regime": (edge.get("regime") or {}).get("regime", "unknown"),
            "market_risk": (edge.get("risk") or {}).get("risk_pct", 0),
            "liquidity": (edge.get("liquidity") or {}).get("state", "unknown"),
            "structure": (edge.get("structure") or {}).get("event", "unknown"),
            "structure_bias": (edge.get("structure") or {}).get("bias", "خنثی"),
            "confluence": (edge.get("confluence") or {}).get("score", 0),
            "confluence_label": (edge.get("confluence") or {}).get("label", ""),
            "meta_probability": (edge.get("meta") or {}).get("probability", 0),
            "meta_label": (edge.get("meta") or {}).get("label", "WATCH"),
            "ai_consensus": (edge.get("ai") or {}).get("agreement", 0),
            "ai_status": (edge.get("ai") or {}).get("status", "NO_AI_DATA"),
            "risk_reward_1": item.get("rr_tp1"), "risk_reward_2": item.get("rr_tp2"),
            "setup_quality": item.get("signal_quality"), "signal_tag": item.get("signal_tag"),
            "governor": (edge.get("governor") or {}).get("state", "NORMAL"),
            "final_state": (edge.get("meta") or {}).get("label", "WATCH"),
        }

    def register_observation(self, item):
        try:
            key = str(item.get("symbol"))
            bucket = self.memory.setdefault("symbols", {}).setdefault(key, {"observations":0,"quality_sum":0.0,"wins":0,"losses":0})
            bucket["observations"] += 1
            bucket["quality_sum"] += safe_float(item.get("signal_quality"), 0)
            self._save_memory()
        except Exception:
            _swallow()

    def historical_win_rate_for_item(self, item):
        try:
            with DB_LOCK, db_conn() as con:
                row = con.execute("SELECT trades,wins FROM setup_stats WHERE setup_key LIKE ? ORDER BY trades DESC LIMIT 1", (f"%{item.get('bias')}%",)).fetchone()
            if row and row[0]: return float(row[1]/row[0]*100)
        except Exception:
            _swallow()
        return 0.0



# ============================================================
# TITAN ADAPTIVE INTELLIGENCE — self-correcting weights & fusion
# Learns from forecast outcomes + AI disagreement patterns.
# ============================================================

class TitanAdaptiveIntelligence:
    """Professional calibration layer.

    Goals:
    - Reduce LONG/SHORT mismatch between quant core and AI ensemble
    - Raise success rate by demanding multi-layer agreement
    - Learn from past WIN/LOSS and shift weights so repeated mistakes fade
    - Never invent edge: disagreement → WAIT unless history strongly favors one side
    """

    def __init__(self):
        self.path = MEMORY_DIR / "adaptive_calibration.json"
        self.state = self._load()

    def _default(self) -> dict[str, Any]:
        return {
            "version": 4,
            "updated_at": 0.0,
            "global": {
                "long_wins": 0, "long_losses": 0,
                "short_wins": 0, "short_losses": 0,
                "ai_agree_wins": 0, "ai_agree_losses": 0,
                "ai_disagree_wins": 0, "ai_disagree_losses": 0,
            },
            "weights": {
                "quant": 0.24,
                "structure": 0.16,
                "regime": 0.12,
                "confluence": 0.14,
                "ai": 0.28,
                "history": 0.06,
            },
            "thresholds": {
                "long_score": 54.0,
                "short_score": 46.0,
                "min_alignment": 50.0,
                "min_confluence": 54.0,
                "ai_consensus_pct": 55.0,
                "promote_score_buffer": 6.0,
            },
            "symbols": {},
            "error_patterns": {
                "false_long": 0,
                "false_short": 0,
                "ignored_ai_long": 0,
                "ignored_ai_short": 0,
            },
        }

    def _load(self) -> dict[str, Any]:
        data = _load_json(self.path, {})
        base = self._default()
        if not isinstance(data, dict):
            return base
        for k, v in base.items():
            if k not in data:
                data[k] = v
            elif isinstance(v, dict) and isinstance(data.get(k), dict):
                for kk, vv in v.items():
                    data[k].setdefault(kk, vv)
        return data

    def _save(self) -> None:
        try:
            self.state["updated_at"] = time.time()
            _save_json(self.path, self.state)
        except Exception as exc:
            LOGGER.debug("adaptive save skipped: %s", exc)

    def _wr(self, wins: float, losses: float) -> float:
        # Beta(2,2) shrinkage prevents 1-3 lucky outcomes from becoming 0%/100% reliability.
        wins = max(0.0, float(wins)); losses = max(0.0, float(losses))
        return ((wins + 2.0) / (wins + losses + 4.0)) * 100.0

    def refresh_from_db(self) -> None:
        """Rebuild calibration memory from DB exactly once per stored outcome.

        The previous implementation reset global counters but kept per-symbol buckets,
        causing the same history to be counted again on every restart/refresh.
        """
        try:
            with DB_LOCK, db_conn() as con:
                rows = con.execute(
                    "SELECT symbol,direction,outcome,ai_majority FROM forecasts "
                    "WHERE outcome IN ('WIN','LOSS') ORDER BY id DESC LIMIT 400"
                ).fetchall()
            self.state["global"] = dict(self._default()["global"])
            self.state["symbols"] = {}
            self.state["error_patterns"] = dict(self._default()["error_patterns"])
            g = self.state["global"]
            for row in rows:
                direction = str(row["direction"]); outcome = str(row["outcome"]); symbol = str(row["symbol"])
                ai_maj = str(row["ai_majority"] or "")
                is_long = direction in {"صعودی", "LONG", "long"}
                is_short = direction in {"نزولی", "SHORT", "short"}
                win = outcome == "WIN"
                if is_long:
                    g["long_wins" if win else "long_losses"] += 1
                elif is_short:
                    g["short_wins" if win else "short_losses"] += 1
                bucket = self.state["symbols"].setdefault(
                    symbol, {"long_wins": 0, "long_losses": 0, "short_wins": 0, "short_losses": 0}
                )
                if is_long:
                    bucket["long_wins" if win else "long_losses"] += 1
                elif is_short:
                    bucket["short_wins" if win else "short_losses"] += 1
                ai_long = ai_maj in {"LONG", "صعودی"}; ai_short = ai_maj in {"SHORT", "نزولی"}
                has_ai = ai_long or ai_short
                ai_agreed = has_ai and ((is_long and ai_long) or (is_short and ai_short))
                if has_ai:
                    g["ai_agree_wins" if (ai_agreed and win) else
                      "ai_agree_losses" if ai_agreed else
                      "ai_disagree_wins" if win else "ai_disagree_losses"] += 1
                    if not win and not ai_agreed:
                        if is_long: self.state["error_patterns"]["false_long"] += 1
                        if is_short: self.state["error_patterns"]["false_short"] += 1
            self._rebalance_weights()
            self._save()
        except Exception as exc:
            LOGGER.debug("adaptive refresh failed: %s", exc)

    def record_outcome(
        self,
        *,
        symbol: str,
        direction: str,
        outcome: str,
        ai_agreed: bool = True,
        quant_was_long: bool = False,
        quant_was_short: bool = False,
    ) -> None:
        g = self.state["global"]
        is_long = direction in {"صعودی", "LONG", "long"}
        is_short = direction in {"نزولی", "SHORT", "short"}
        win = outcome == "WIN"
        loss = outcome == "LOSS"
        if not (win or loss):
            return
        if is_long:
            g["long_wins" if win else "long_losses"] += 1
        elif is_short:
            g["short_wins" if win else "short_losses"] += 1
        if ai_agreed:
            g["ai_agree_wins" if win else "ai_agree_losses"] += 1
        else:
            g["ai_disagree_wins" if win else "ai_disagree_losses"] += 1
            if loss and is_long:
                self.state["error_patterns"]["false_long"] = int(self.state["error_patterns"].get("false_long", 0)) + 1
            if loss and is_short:
                self.state["error_patterns"]["false_short"] = int(self.state["error_patterns"].get("false_short", 0)) + 1
            # Track when quant ignored AI and lost
            if loss and quant_was_long and not is_long:
                pass
            if loss and not ai_agreed:
                if is_long:
                    self.state["error_patterns"]["ignored_ai_short"] = int(self.state["error_patterns"].get("ignored_ai_short", 0)) + 1
                if is_short: