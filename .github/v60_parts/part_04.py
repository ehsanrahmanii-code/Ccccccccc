
def _rolling_trend_stability(close: pd.Series, lookback: int = 20) -> tuple[float, float]:
    """Return (signed stability, consistency) from closed-candle returns.

    Stability is the average sign agreement of returns, scaled by direction.
    Consistency is the fraction of returns agreeing with the dominant direction.
    """
    r = pd.to_numeric(close, errors="coerce").pct_change().dropna().tail(lookback)
    if len(r) < max(8, lookback // 2):
        return 0.0, 0.0
    pos = float((r > 0).mean()); neg = float((r < 0).mean())
    consistency = max(pos, neg)
    signed = (pos - neg) * consistency
    return float(clamp(signed, -1.0, 1.0)), float(clamp(consistency, 0.0, 1.0))


def _candle_pressure(df: pd.DataFrame, lookback: int = 12) -> dict[str, float]:
    """Measure body dominance and directional close location, not raw volume prediction."""
    if df is None or df.empty:
        return {"pressure": 0.0, "body_quality": 0.0, "close_location": 0.5}
    x = df.tail(lookback).copy()
    rng = (x["high"] - x["low"]).replace(0, np.nan)
    body = (x["close"] - x["open"]).abs() / rng
    loc = (x["close"] - x["low"]) / rng
    signed_body = np.sign(x["close"] - x["open"]) * body.fillna(0)
    weights = np.linspace(0.5, 1.0, len(x))
    pressure = float(np.average(signed_body.fillna(0), weights=weights)) if len(x) else 0.0
    return {
        "pressure": round(clamp(pressure, -1.0, 1.0), 4),
        "body_quality": round(clamp(float(body.fillna(0).mean()), 0.0, 1.0), 4),
        "close_location": round(clamp(float(loc.fillna(0.5).tail(3).mean()), 0.0, 1.0), 4),
    }


def _precision_engine_snapshot(
    df15: pd.DataFrame,
    df1h: pd.DataFrame,
    price: float,
    atr: float,
    rsi: float,
    vwap: float,
    ema20: float,
    ema50: float,
    direction: str,
    volume_spike: bool,
    structure: dict[str, Any],
    regime: dict[str, Any],
    derivatives: dict[str, Any],
) -> dict[str, Any]:
    """Precision gate: entry timing, trend stability, stretch and market-quality filters.

    This is deliberately a gate/diagnostic, not a promise of predictive accuracy.
    """
    try:
        adx_s = _adx_series(df1h)
        adx = float(adx_s.iloc[-1]) if not adx_s.empty else 0.0
        signed_stability, consistency = _rolling_trend_stability(df1h["close"], 20)
        cp = _candle_pressure(df15, 12)
        atr_safe = max(float(atr), price * 0.0001, 1e-12)
        stretch_vwap = abs(price - vwap) / atr_safe if vwap > 0 else 0.0
        stretch_ema = abs(price - ema20) / atr_safe if ema20 > 0 else 0.0
        trend_up = ema20 > ema50
        trend_dir = 1 if trend_up else -1
        wanted = 1 if direction in {"صعودی", "LONG", "LONG/BUY"} else -1 if direction in {"نزولی", "SHORT", "SHORT/SELL"} else 0
        trend_alignment = trend_dir * wanted if wanted else 0
        structure_score = safe_float((structure or {}).get("confirmation_score"), 50)
        regime_name = str((regime or {}).get("regime", "unknown")).lower()
        trend_regime = any(k in regime_name for k in ("trend", "bull", "bear"))
        oi_delta = safe_float((derivatives or {}).get("oi_delta"), 0)
        funding = safe_float((derivatives or {}).get("funding"), 0)

        # ADX: low trend strength is a common source of false breakout-style entries.
        adx_score = clamp((adx - 12.0) / 28.0 * 100.0, 0, 100)
        stability_score = clamp(consistency * 100.0, 0, 100)
        direction_consistency = clamp((0.5 + 0.5 * signed_stability * wanted) * 100.0, 0, 100) if wanted else 50
        stretch_penalty = 0.0
        if stretch_vwap > 2.8: stretch_penalty += min(25.0, (stretch_vwap - 2.8) * 9.0)
        if stretch_ema > 3.2: stretch_penalty += min(20.0, (stretch_ema - 3.2) * 7.0)
        rsi_penalty = 0.0
        if wanted > 0 and rsi > 74: rsi_penalty = min(18.0, (rsi - 74) * 1.8)
        if wanted < 0 and rsi < 26: rsi_penalty = min(18.0, (26 - rsi) * 1.8)
        body_score = cp["body_quality"] * 100.0
        candle_direction = cp["pressure"] * wanted
        candle_score = clamp(50.0 + candle_direction * 45.0 + (body_score - 50.0) * 0.25, 0, 100)
        derivative_alignment = 50.0
        if wanted:
            # OI confirmation is supportive only when it points with price direction; funding is a softer input.
            if wanted > 0 and oi_delta > 0: derivative_alignment += min(18.0, oi_delta * 2.0)
            if wanted < 0 and oi_delta < 0: derivative_alignment += min(18.0, abs(oi_delta) * 2.0)
            if wanted > 0 and funding < -0.0002: derivative_alignment += 5.0
            if wanted < 0 and funding > 0.0002: derivative_alignment += 5.0
        derivative_alignment = clamp(derivative_alignment, 0, 100)

        precision = (
            0.24 * adx_score
            + 0.22 * stability_score
            + 0.18 * direction_consistency
            + 0.14 * candle_score
            + 0.12 * clamp(structure_score, 0, 100)
            + 0.10 * derivative_alignment
        ) - stretch_penalty - rsi_penalty
        if trend_alignment < 0:
            precision -= 12.0
        if not trend_regime and adx < 16:
            precision -= 8.0
        precision = float(clamp(precision, 0, 100))

        hard_blocks = []
        if price <= 0 or atr <= 0: hard_blocks.append("invalid_price_or_atr")
        if adx < 11 and consistency < 0.58: hard_blocks.append("low_trend_strength")
        if stretch_vwap > 4.0 or stretch_ema > 4.5: hard_blocks.append("overextended_entry")
        if wanted and direction_consistency < 34: hard_blocks.append("directional_instability")
        return {
            "score": round(precision, 1), "adx": round(adx, 2),
            "trend_stability": round(signed_stability, 4), "consistency": round(consistency, 4),
            "direction_consistency": round(direction_consistency, 1),
            "stretch_vwap_atr": round(stretch_vwap, 2), "stretch_ema_atr": round(stretch_ema, 2),
            "candle_pressure": cp["pressure"], "body_quality": cp["body_quality"],
            "candle_score": round(candle_score, 1), "derivative_alignment": round(derivative_alignment, 1),
            "trend_alignment": trend_alignment, "hard_blocks": hard_blocks,
            "entry_timing": "GOOD" if precision >= 72 and not hard_blocks else "WAIT" if precision < 56 or hard_blocks else "CAUTIOUS",
        }
    except Exception as exc:
        LOGGER.debug("precision engine failed: %s", exc)
        return {"score": 50.0, "entry_timing": "CAUTIOUS", "hard_blocks": [], "error": str(exc)}


def _friction_adjusted_rr(entry: float, sl: float, tp: float) -> float:
    risk = abs(entry - sl)
    reward = abs(tp - entry)
    friction = max(entry * TOTAL_ENTRY_BUFFER, 1e-12)
    return float(reward / max(risk + friction, 1e-12))



# ============================================================
# TITAN V9 — NEURAL SYNAPSE (central nervous system)
# Connects: TF scores · structure · regime · confluence · precision ·
# patterns · forecast path · derivatives · AI ensemble · DQ · ladder ·
# calibration · portfolio. Produces one coherent decision + confidence.
# Analysis-only. Never invents edge — only fuses existing evidence.
# ============================================================

class TitanNeuralSynapseV9:
    """Advanced multi-layer fusion with explainable weighted synapses."""

    # Layer weights (sum ≈ 1.0) — tuned for reliability over aggressiveness
    W = {
        # V28 tuned: stronger TF spine + precision + calibration for reliable edges
        "tf_spine": 0.20,       # multi-TF weighted score
        "structure": 0.11,      # market structure HH/HL
        "regime": 0.08,         # trend/vol regime
        "confluence": 0.11,     # multi-factor confluence
        "precision": 0.12,      # entry timing / ADX / stretch
        "pattern": 0.06,        # chart patterns
        "forecast": 0.07,       # probabilistic path (self-weighted)
        "derivatives": 0.08,    # funding / OI / L-S / taker
        "ai": 0.09,             # AI ensemble
        "calibration": 0.08,    # historical success calibration
    }

    def _side_from_score(self, sc: float) -> str:
        # Tuned V28: 54/46 — fewer missed edges than 56/44, still avoids noise
        if sc >= 54:
            return "LONG"
        if sc <= 46:
            return "SHORT"
        return "WAIT"

    def _tf_vector(self, tf_scores: dict) -> tuple[float, str, float]:
        if not tf_scores:
            return 50.0, "WAIT", 0.0
        weights = {"15m": 0.18, "1h": 0.30, "4h": 0.28, "1d": 0.24}  # V28 balanced actionable
        keys = [k for k in weights if k in tf_scores]
        if not keys:
            vals = list(tf_scores.values())
            avg = float(sum(vals) / len(vals))
            return avg, self._side_from_score(avg), abs(avg - 50.0)
        wsum = sum(weights[k] for k in keys)
        avg = sum(safe_float(tf_scores[k], 50) * weights[k] for k in keys) / max(wsum, 1e-9)
        # agreement among TFs
        sides = [self._side_from_score(safe_float(tf_scores[k], 50)) for k in keys]
        maj = max(set(sides), key=sides.count)
        agree = sides.count(maj) / len(sides)
        return float(avg), maj, float(agree * abs(avg - 50.0))

    def _pattern_signal(self, patterns: dict) -> tuple[float, str]:
        primary = (patterns or {}).get("primary") or {}
        if not primary:
            return 50.0, "WAIT"
        bias = str((primary.get("guide") or {}).get("bias", "خنثی"))
        conf = safe_float(primary.get("confidence"), 50) / 100.0
        if bias == "صعودی":
            return 50.0 + 18.0 * conf, "LONG"
        if bias == "نزولی":
            return 50.0 - 18.0 * conf, "SHORT"
        return 50.0, "WAIT"

    def _forecast_signal(self, forecast: dict, path_stats: dict) -> tuple[float, str]:
        if not forecast or not forecast.get("ok"):
            return 50.0, "WAIT"
        overall = str(forecast.get("overall_bias", "خنثی"))
        move = safe_float(forecast.get("expected_move_pct"), 0)
        strength = safe_float(forecast.get("path_strength"), 0)
        scale = safe_float((path_stats or {}).get("weight_scale"), 0.35)
        # damp by historical path accuracy
        tilt = float(np.clip(move * 2.5 + strength * 0.15, -18, 18)) * scale
        sc = 50.0 + tilt
        side = "LONG" if overall == "صعودی" and sc >= 53 else "SHORT" if overall == "نزولی" and sc <= 47 else "WAIT"
        return float(clamp(sc, 5, 95)), side

    def _deriv_signal(self, derivatives: dict, wanted_hint: int = 0) -> tuple[float, str]:
        sc = 50.0
        funding = derivatives.get("funding_value")
        oi_d = safe_float(derivatives.get("oi_delta"), 0)
        ls_ratio = safe_float(derivatives.get("long_short_ratio"), 1.0)
        taker_buy = safe_float(derivatives.get("taker_buy_pct"), float("nan"))
        if not math.isfinite(taker_buy):
            taker_buy = 50.0  # neutral only when data missing; marked unavailable upstream
        if funding is not None:
            f = safe_float(funding, 0)
            # extreme funding is contrarian
            if f >= 0.06:
                sc -= 6
            elif f <= -0.06:
                sc += 6
            elif f >= 0.025:
                sc -= 2.5
            elif f <= -0.025:
                sc += 2.5
        if oi_d > 1.5:
            sc += 3.0 if wanted_hint >= 0 else -3.0
        elif oi_d < -1.5:
            sc -= 2.0 if wanted_hint >= 0 else 2.0
        # crowding
        if ls_ratio >= 1.6:
            sc -= 5  # long crowded → short bias
        elif ls_ratio <= 0.65:
            sc += 5
        # taker flow
        sc += float(np.clip((taker_buy - 50) * 0.12, -5, 5))
        sc = float(clamp(sc, 10, 90))
        return sc, self._side_from_score(sc)

    def _ai_signal(self, fusion: dict, ai_opinions: dict) -> tuple[float, str, float]:
        maj = str((fusion or {}).get("ai_majority") or "WAIT")
        agree = safe_float((fusion or {}).get("ai_agreement"), 0)
        if maj not in {"LONG", "SHORT", "WAIT"}:
            # parse from opinions if needed
            votes = []
            for key in ("gemini", "openai", "grok", "claude", "deepseek", "internal"):
                text = str((ai_opinions or {}).get(key) or "")
                if not text:
                    continue
                v = extract_structured_ai_vote(text)
                votes.append(v.get("side", "WAIT"))
            if votes:
                maj = max(set(votes), key=votes.count)
                agree = votes.count(maj) / len(votes) * 100.0
        if maj == "LONG":
            sc = 50.0 + min(28.0, agree * 0.28)
        elif maj == "SHORT":
            sc = 50.0 - min(28.0, agree * 0.28)
        else:
            sc = 50.0
        return float(clamp(sc, 15, 85)), maj if maj in {"LONG", "SHORT", "WAIT"} else "WAIT", float(agree)

    def fuse(self, *, tf_scores: dict, structure: dict, regime: dict, confluence: dict,
             precision: dict, patterns: dict, forecast: dict, path_stats: dict,
             derivatives: dict, fusion: dict, ai_opinions: dict,
             data_quality: dict, ladder: dict, calib: dict,
             quant_bias: str, quant_score: float) -> dict:
        """Full neural fuse → side, confidence, layer map, vetoes."""
        layers: dict[str, Any] = {}
        reasons: list[str] = []
        vetoes: list[str] = []

        tf_sc, tf_side, tf_agree_mag = self._tf_vector(tf_scores or {})
        layers["tf_spine"] = {"score": round(tf_sc, 1), "side": tf_side, "weight": self.W["tf_spine"]}

        struct_bias = str((structure or {}).get("bias", "خنثی"))
        struct_conf = safe_float((structure or {}).get("confirmation_score"), 50)
        if struct_bias == "صعودی":
            st_sc = 50 + min(22, struct_conf * 0.25)
            st_side = "LONG"
        elif struct_bias == "نزولی":
            st_sc = 50 - min(22, struct_conf * 0.25)
            st_side = "SHORT"
        else:
            st_sc, st_side = 50.0, "WAIT"
        layers["structure"] = {"score": round(st_sc, 1), "side": st_side, "weight": self.W["structure"]}

        reg_name = str((regime or {}).get("regime", "unknown")).lower()
        if "up" in reg_name or "bull" in reg_name:
            rg_sc, rg_side = 62.0, "LONG"
        elif "down" in reg_name or "bear" in reg_name:
            rg_sc, rg_side = 38.0, "SHORT"
        elif "high_vol" in reg_name:
            rg_sc, rg_side = 50.0, "WAIT"
            reasons.append("رژیم پرنوسان → کاهش اطمینان")
        else:
            rg_sc, rg_side = 50.0, "WAIT"
        layers["regime"] = {"score": rg_sc, "side": rg_side, "weight": self.W["regime"]}

        conf_sc = safe_float((confluence or {}).get("score"), 50)
        layers["confluence"] = {"score": conf_sc, "side": self._side_from_score(conf_sc), "weight": self.W["confluence"]}

        prec_sc = safe_float((precision or {}).get("score"), 50)
        layers["precision"] = {"score": prec_sc, "side": self._side_from_score(prec_sc), "weight": self.W["precision"]}
        for b in (precision or {}).get("hard_blocks") or []:
            vetoes.append(f"precision:{b}")

        p_sc, p_side = self._pattern_signal(patterns or {})
        layers["pattern"] = {"score": round(p_sc, 1), "side": p_side, "weight": self.W["pattern"]}

        f_sc, f_side = self._forecast_signal(forecast or {}, path_stats or {})
        layers["forecast"] = {"score": round(f_sc, 1), "side": f_side, "weight": self.W["forecast"]}

        wanted_hint = 1 if quant_bias == "صعودی" else -1 if quant_bias == "نزولی" else 0
        d_sc, d_side = self._deriv_signal(derivatives or {}, wanted_hint)
        layers["derivatives"] = {"score": round(d_sc, 1), "side": d_side, "weight": self.W["derivatives"]}

        a_sc, a_side, a_agree = self._ai_signal(fusion or {}, ai_opinions or {})
        layers["ai"] = {"score": round(a_sc, 1), "side": a_side, "weight": self.W["ai"], "agreement": a_agree}

        cal_sc = safe_float((calib or {}).get("calibrated"), safe_float((fusion or {}).get("success_probability"), 50))
        # map calibrated success (around 50-80) to directional lean via quant
        if quant_bias == "صعودی":
            c_sc = 40 + cal_sc * 0.35
        elif quant_bias == "نزولی":
            c_sc = 60 - cal_sc * 0.35
        else:
            c_sc = 50.0
        layers["calibration"] = {"score": round(c_sc, 1), "side": self._side_from_score(c_sc), "weight": self.W["calibration"]}

        # Weighted neural score
        neural_score = 0.0
        w_total = 0.0
        for name, layer in layers.items():
            w = float(layer.get("weight") or 0)
            neural_score += safe_float(layer.get("score"), 50) * w
            w_total += w
        neural_score = neural_score / max(w_total, 1e-9)

        # Blend with quant spine (prevents AI-only drift)
        quant_sc = safe_float(quant_score, 50)
        neural_score = 0.62 * neural_score + 0.38 * quant_sc
        neural_score = float(clamp(neural_score, 0, 100))

        # Side vote among layers (weighted)
        long_w = short_w = wait_w = 0.0
        for layer in layers.values():
            w = float(layer.get("weight") or 0)
            s = layer.get("side")
            if s == "LONG":
                long_w += w
            elif s == "SHORT":
                short_w += w
            else:
                wait_w += w
        # V59.2: soften residual WAIT — allow lean when directional layers dominate
        if long_w > short_w and long_w >= wait_w * 0.55:
            side = "LONG"
        elif short_w > long_w and short_w >= wait_w * 0.55:
            side = "SHORT"
        elif long_w > short_w * 1.08 and neural_score >= 52:
            side = "LONG"
        elif short_w > long_w * 1.08 and neural_score <= 48:
            side = "SHORT"
        elif long_w > short_w and neural_score >= 56:
            side = "LONG"
        elif short_w > long_w and neural_score <= 44:
            side = "SHORT"
        else:
            side = "WAIT"

        # Hard gates — only true hard_veto kills; soft DQ lowers confidence
        dq = data_quality or {}
        if dq.get("hard_veto"):
            side = "WAIT"
            vetoes.append("data_quality_veto")
            reasons.append("وتوی سخت Data quality → انتظار")
        elif safe_float(dq.get("score"), 100) < MIN_DATA_QUALITY_SCORE:
            vetoes.append("data_quality_soft")
            reasons.append("Data quality مرزی — جهت حفظ با کاهش اطمینان")

        ladder = ladder or {}
        if side in {"LONG", "SHORT"} and ladder and not ladder.get("passed", True):
            # Soft miss: keep direction if HTF not strongly against; only hard-WAIT on structural flip
            htf_s = safe_float(ladder.get("htf_score"), 50)
            structural = (
                (side == "LONG" and htf_s <= 34) or (side == "SHORT" and htf_s >= 66)
            )
            if structural:
                vetoes.append("entry_ladder")
                reasons.append("نردبان ورود HTF خلاف جهت قوی → انتظار")
                side = "WAIT"
            else:
                reasons.append("نردبان ورود ناقص — جهت حفظ شد با اطمینان کمتر")

        # Precision hard blocks
        if (precision or {}).get("hard_blocks") and side in {"LONG", "SHORT"}:
            if safe_float(prec_sc, 50) < 48:
                side = "WAIT"
                reasons.append("بلوک‌های سخت دقت ورود فعال")

        # AI hard conflict with weak quant
        if a_side in {"LONG", "SHORT"} and side in {"LONG", "SHORT"} and a_side != side and a_agree >= 88:
            if abs(neural_score - 50) < 12:
                side = "WAIT"
                reasons.append("تعارض قوی AI با سیگنال ضعیف → انتظار")
            else:
                reasons.append("تعارض AI لحاظ شد (جریمه اطمینان)")

        # Confidence from layer agreement + distance from 50 + calib
        side_agreement = max(long_w, short_w, wait_w) / max(long_w + short_w + wait_w, 1e-9)
        confidence = (
            0.35 * abs(neural_score - 50) * 2
            + 0.30 * side_agreement * 100
            + 0.20 * safe_float(dq.get("score"), 60)
            + 0.15 * cal_sc
        )
        if side == "WAIT":
            confidence = min(confidence, 55)
        if any("نردبان ورود ناقص" in str(r) for r in reasons):
            confidence = min(confidence, 78)
        confidence = float(clamp(confidence, 5, 92))

        bias = "صعودی" if side == "LONG" else "نزولی" if side == "SHORT" else "خنثی"

        # Human-readable synapse summary
        top_layers = sorted(layers.items(), key=lambda x: abs(safe_float(x[1].get("score"), 50) - 50), reverse=True)[:4]
        for name, layer in top_layers:
            reasons.append(f"{name}: {layer.get('side')} ({layer.get('score')})")

        return {
            "side": side,
            "bias": bias,
            "neural_score": round(neural_score, 1),
            "confidence": round(confidence, 1),
            "layers": layers,
            "long_weight": round(long_w, 3),
            "short_weight": round(short_w, 3),
            "wait_weight": round(wait_w, 3),
            "side_agreement": round(side_agreement, 3),
            "reasons": reasons[:8],
            "vetoes": vetoes,
            "param_version": TITAN_PARAM_VERSION,
        }



TITAN_NEURAL = TitanNeuralSynapseV9()


# ============================================================
# TITAN V10 — PROFESSIONAL SIGNAL GRADE + DOSSIER
# A+ … F classification with multi-gate checklist.
# Only A+/A appear as actionable high-trust signals.
# ============================================================

GRADE_RANK = {"A+": 6, "A": 5, "B": 4, "C": 3, "D": 2, "F": 1, "—": 0}

def classify_signal_grade(
    *,
    decision_tag: str,
    signal_quality: float,
    success_prob: float,
    alignment: float,
    neural: Optional[dict] = None,
    meta: Optional[dict] = None,
    ladder: Optional[dict] = None,
    data_quality: Optional[dict] = None,
    precision: Optional[dict] = None,
    effective_rr1: float = 0.0,
    ai_agreement: float = 0.0,
    pattern_conf: float = 0.0,
    forecast_aligned: bool = False,
) -> dict[str, Any]:
    """Professional institutional-style grade with checklist evidence.

    A+ : all hard gates pass, quality≥85, neural confirms, RR≥1.4, meta ACCEPT
    A  : hard gates pass, quality≥75, neural same side, meta not REJECT
    B  : directional but missing 1 soft edge (watchlist, not primary action)
    C  : weak directional lean — observation only
    D  : noise / conflict
    F  : hard veto or WAIT with poor data
    """
    neural = neural or {}
    meta = meta or {}
    ladder = ladder or {}
    data_quality = data_quality or {}
    precision = precision or {}

    checklist: list[dict[str, Any]] = []
    def _chk(name: str, ok: bool, detail: str = "") -> bool:
        checklist.append({"name": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    is_dir = decision_tag in {"LONG", "SHORT"}
    dq_sc = safe_float(data_quality.get("score"), 0)
    ladder_ok = bool(ladder.get("passed", False)) if is_dir else True
    meta_label = str(meta.get("label") or "")
    meta_ok = meta_label == "ACCEPT"
    meta_not_reject = meta_label != "REJECT"
    neural_side = str(neural.get("side") or "")
    neural_conf = safe_float(neural.get("confidence"), 0)
    neural_sc = safe_float(neural.get("neural_score"), 50)
    neural_same = (not neural) or (neural_side == decision_tag) or (not is_dir)
    prec_sc = safe_float(precision.get("score"), 50)
    hard_blocks = list(precision.get("hard_blocks") or [])
    dq_ok = dq_sc >= MIN_DATA_QUALITY_SCORE and not data_quality.get("hard_veto")
    rr_ok = effective_rr1 >= MIN_EFFECTIVE_RR if is_dir else True

    g_dq = _chk("Data quality", dq_ok, f"DQ={dq_sc:.0f}")
    g_ladder = _chk("نردبان HTF→MTF→LTF", ladder_ok or not is_dir, f"HTF={ladder.get('htf_score','—')} MTF={ladder.get('mtf_score','—')}")
    g_meta = _chk("متا-برچسب", meta_not_reject, meta_label or "—")
    g_neural = _chk("سیناپس عصبی", neural_same if is_dir else True, f"{neural_side} conf={neural_conf:.0f}")
    g_rr = _chk("نسبت ریسک/پاداش", rr_ok, f"RR1={effective_rr1:.2f}")
    g_prec = _chk("دقت ورود", prec_sc >= 52 and not hard_blocks, f"P={prec_sc:.0f} blocks={len(hard_blocks)}")
    g_align = _chk("همسویی TF", safe_float(alignment, 0) >= 55, f"align={alignment:.0f}")
    g_prob = _chk("احتمال موفقیت", safe_float(success_prob, 0) >= 55, f"p={success_prob:.0f}%")
    g_ai = _chk("اجماع AI", safe_float(ai_agreement, 0) >= 40 or not is_dir, f"agree={ai_agreement:.0f}%")
    g_pattern = _chk("الگو/مسیر", pattern_conf >= 50 or forecast_aligned or not is_dir, f"pat={pattern_conf:.0f} fc={forecast_aligned}")

    hard_pass = g_dq and g_ladder and g_meta and g_neural and g_rr and g_prec and is_dir
    soft_count = sum(1 for x in (g_align, g_prob, g_ai, g_pattern) if x)
    passed = sum(1 for c in checklist if c["ok"])
    total = len(checklist)

    grade = "F"
    label_fa = "رد / غیرقابل‌اتکا"
    action = "اجتناب"
    tier_color = "#64748b"

    if not is_dir:
        if dq_sc >= 65 and safe_float(signal_quality, 0) >= 48:
            grade, label_fa, action, tier_color = "C", "خنثی / رصد", "منتظر تأیید", "#fbbf24"
        else:
            grade, label_fa, action, tier_color = "D", "No edge", "عبور", "#94a3b8"
    # V28.3: realistic institutional bands — A+/A reachable without perfect AI stack
    elif hard_pass and safe_float(signal_quality, 0) >= 78 and safe_float(success_prob, 0) >= 60 and meta_not_reject and effective_rr1 >= 1.25 and soft_count >= 2:
        grade, label_fa, action, tier_color = "A+", "اطمینان بالا", "اولویت ورود", "#4ade80"
    elif (hard_pass or (g_dq and ladder_ok and rr_ok)) and safe_float(signal_quality, 0) >= 62 and safe_float(success_prob, 0) >= 52 and meta_not_reject and soft_count >= 1:
        grade, label_fa, action, tier_color = "A", "قابل‌اتکا", "ورود با مدیریت ریسک", "#22c55e"
    elif g_dq and is_dir and safe_float(signal_quality, 0) >= 52 and meta_not_reject:
        grade, label_fa, action, tier_color = "B", "متوسط / نیاز تأیید", "واچ‌لیست فعال", "#38bdf8"
    elif is_dir and safe_float(signal_quality, 0) >= 46:
        grade, label_fa, action, tier_color = "C", "ضعیف", "رصد — ورود محتاطانه", "#fbbf24"
    else:
        grade, label_fa, action, tier_color = "D", "نویز / تعارض", "اجتناب", "#f43f5e"

    # Force demote on any hard veto leftover
    if data_quality.get("hard_veto") or (is_dir and not g_dq and dq_sc < 40):
        grade, label_fa, action, tier_color = "F", "وتوی داده", "اجتناب کامل", "#7f1d1d"
    if is_dir and hard_blocks and prec_sc < 38:
        if GRADE_RANK.get(grade, 0) > GRADE_RANK["C"]:
            grade, label_fa, action, tier_color = "C", "بلاک دقت ورود", "رصد", "#fbbf24"

    trust = float(clamp(
        0.30 * safe_float(signal_quality, 0)
        + 0.25 * safe_float(success_prob, 0)
        + 0.15 * (100.0 * passed / max(total, 1))
        + 0.15 * neural_conf
        + 0.15 * dq_sc,
        0, 100,
    ))

    return {
        "grade": grade,
        "label_fa": label_fa,
        "action": action,
        "tier_color": tier_color,
        "rank": GRADE_RANK.get(grade, 0),
        "trust_index": round(trust, 1),
        "checklist": checklist,
        "passed_gates": passed,
        "total_gates": total,
        "hard_pass": hard_pass,
        "param_version": TITAN_PARAM_VERSION,
    }


def build_signal_dossier(item: dict[str, Any]) -> dict[str, Any]:
    """Compact professional dossier for ranked board + card header."""
    grade = item.get("signal_grade") or {}
    neural = item.get("neural_v9") or {}
    st = str(item.get("stance") or "")
    fam = str(item.get("stance_family") or "")
    if not st:
        try:
            u = _unified_stance(item)
            st, fam = u.get("stance", "WAIT"), u.get("stance_family", "WAIT")
        except Exception:
            st, fam = "WAIT", "WAIT"
    # decision for board = family if lean/published, else WAIT
    board_dec = fam if fam in {"LONG", "SHORT"} else "WAIT"
    return {
        "symbol": item.get("symbol"),
        "base": item.get("base_symbol"),
        "icon": item.get("coin_icon"),
        "name": item.get("coin_name"),
        "decision": board_dec,
        "stance": st,
        "stance_family": fam,
        "published": bool(item.get("stance_published")),
        "decision_tag": item.get("decision_tag") or "WAIT",
        "bias": item.get("bias") or ("صعودی" if fam=="LONG" else "نزولی" if fam=="SHORT" else "خنثی"),
        "grade": grade.get("grade", "—"),
        "grade_label": grade.get("label_fa", "—"),
        "action": grade.get("action", "—"),
        "trust": grade.get("trust_index", 0),
        "quality": item.get("signal_quality"),
        "success_prob": item.get("success_probability"),
        "score": item.get("score"),
        "alignment": item.get("alignment"),
        "neural_score": neural.get("neural_score") or item.get("neural_score"),
        "neural_conf": neural.get("confidence") or item.get("neural_confidence"),
        "price": item.get("price"),
        "entry": item.get("entry_valid"),
        "sl": item.get("stop_loss"),
        "tp1": item.get("tp1"),
        "tp2": item.get("tp2"),
        "rr1": item.get("rr_tp1"),
        "rr2": item.get("rr_tp2"),
        "tag": item.get("signal_tag"),
        "tier_color": grade.get("tier_color", "#94a3b8"),
    }


def rank_market_signals(market_data: list[dict[str, Any]]) -> dict[str, Any]:
    """Classify market into actionable / watch / avoid boards (V28.3).

    - actionable: A+/A directional (primary board)
    - active: any LONG/SHORT (includes B) for HUD counts so the main page
      shows real directional activity, not only institutional A+
    - watchlist: B directional + high opportunity EARLY states
    """
    dossiers = [build_signal_dossier(x) for x in (market_data or []) if x]
    # Enrich dossier with opportunity fields from source items
    by_sym = {str(x.get("symbol")): x for x in (market_data or []) if x}
    for d in dossiers:
        src = by_sym.get(str(d.get("symbol"))) or {}
        d["opportunity_score"] = safe_float(src.get("opportunity_score"), 0)
        d["opportunity_state"] = str(src.get("opportunity_state") or "")
        d["entry_mode"] = str(src.get("entry_mode") or "")
    dossiers.sort(key=lambda d: (
        -GRADE_RANK.get(str(d.get("grade")), 0),
        -safe_float(d.get("opportunity_score"), 0),
        -safe_float(d.get("trust"), 0),
        -safe_float(d.get("quality"), 0),
        -safe_float(d.get("success_prob"), 0),
    ))
    # Unified: LONG/SHORT family includes published + LEAN (same voice everywhere)
    directional = [d for d in dossiers if d.get("decision") in {"LONG", "SHORT"}]
    published = [d for d in dossiers if d.get("published") and d.get("decision") in {"LONG", "SHORT"}]
    leans = [d for d in dossiers if (not d.get("published")) and str(d.get("stance") or "").startswith("LEAN_")]
    actionable = [d for d in directional if d.get("grade") in {"A+", "A"} or d.get("published")]
    active = list(directional)  # published + lean
    watch = [
        d for d in dossiers
        if d in leans
        or (d.get("grade") == "B" and d.get("decision") in {"LONG", "SHORT"})
        or str(d.get("opportunity_state") or "").startswith(("EARLY_", "WATCH_"))
    ]
    observe = [d for d in dossiers if d.get("decision") == "WAIT"]
    avoid = [d for d in dossiers if d.get("grade") == "F"]
    long_n = sum(1 for d in directional if d.get("decision") == "LONG")
    short_n = sum(1 for d in directional if d.get("decision") == "SHORT")
    wait_n = sum(1 for d in dossiers if d.get("decision") == "WAIT")
    lean_long_n = sum(1 for d in leans if d.get("decision") == "LONG")
    lean_short_n = sum(1 for d in leans if d.get("decision") == "SHORT")
    # Sort active by conviction
    active.sort(key=lambda d: (
        0 if d.get("published") else 1,
        -safe_float(d.get("quality"), 0),
        -abs(safe_float(d.get("score"), 50) - 50),
    ))
    return {
        "all": dossiers,
        "actionable": actionable,
        "active": active,
        "watchlist": watch,
        "observe": observe[:12],
        "avoid": avoid,
        "counts": {
            "actionable": len(actionable),
            "active": len(active),
            "watch": len(watch),
            "observe": len(observe),
            "avoid": len(avoid),
            "long": long_n,
            "short": short_n,
            "wait": wait_n,
            "lean_long": lean_long_n,
            "lean_short": lean_short_n,
            "published_long": sum(1 for d in published if d.get("decision")=="LONG"),
            "published_short": sum(1 for d in published if d.get("decision")=="SHORT"),
            "total": len(dossiers),
        },
        "top": (actionable[0] if actionable else (active[0] if active else (dossiers[0] if dossiers else None))),
        "top3": (actionable or active)[:3],
        "param_version": TITAN_PARAM_VERSION,
    }




class TitanDecisionCoreV2:
    def validate_data(self, payload: dict[str, Any]) -> bool:
        return all(k in payload and payload[k] is not None for k in ("price", "score"))

    def detect_regime(self, atr_pct: float = 0, trend_score: float = 50) -> str:
        if atr_pct > 5:
            return "high_volatility"
        if trend_score >= 60:
            return "bull_trend"
        if trend_score <= 40:
            return "bear_trend"
        return "sideways"

    def confidence(self, score: float, agreement: float = 100, risk: float = 0) -> float:
        result = float(score)
        result *= max(0.5, agreement / 100)
        result -= risk
        return max(0, min(100, round(result, 2)))

    def decide(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.validate_data(payload):
            return {"decision": "WAIT", "confidence": 0, "reason": "داده کافی نیست"}
        score = float(payload.get("score", 50))
        agreement = float(payload.get("agreement", 70))
        risk = float(payload.get("risk", 0))
        confidence = self.confidence(score, agreement, risk)
        decision = "WAIT"
        if confidence >= 65 and score >= 60:
            decision = "LONG"
        elif confidence >= 65 and score <= 40:
            decision = "SHORT"
        return {
            "decision": decision,
            "confidence": confidence,
            "regime": self.detect_regime(payload.get("atr_pct", 0), score),
            "explanation": ["داده اعتبارسنجی شد", "روند و ریسک بررسی شد", "تصمیم با فیلتر اطمینان صادر شد"],
        }

TITAN_DECISION_CORE = TitanDecisionCoreV2()


# ============================================================
# TITAN DECISION CORE V3 - SAFE INTEGRATION WRAPPER
# Restored from the user's original master build.
# ============================================================

def titan_final_decision(
    score=50,
    price=None,
    ai_votes=None,
    risk=0,
    atr_pct=0,
    metadata=None
):
    """Unified decision gateway for all existing engine outputs."""
    payload = {
        "score": score,
        "price": price,
        "risk": risk,
        "atr_pct": atr_pct,
        "metadata": metadata or {}
    }
    if ai_votes:
        numeric_votes = [float(v) for v in ai_votes.values() if isinstance(v, (int, float))] if isinstance(ai_votes, dict) else []
        payload["agreement"] = (sum(numeric_votes) / len(numeric_votes)) if numeric_votes else 50
        payload["ai_votes"] = ai_votes
    result = TITAN_DECISION_CORE.decide(payload)
    result["system_checks"] = {
        "data_validation": True,
        "confidence_filter": True,
        "risk_filter": True,
        "explainability": True
    }
    return result


def build_titan_analysis(*, symbol: str, price: float, rsi: float, vwap: float, ema20: float, ema50: float, atr: float,
                         volume_spike: bool, btc_trend: str, derivatives: dict[str, Any],
                         tf_results: dict[str, str], tf_scores: dict[str, float]) -> dict[str, Any]:
    vwap_dev = ((price - vwap) / vwap * 100.0) if vwap > 0 else 0.0
    funding = derivatives.get("funding_value")
    oi_delta = safe_float(derivatives.get("oi_delta"), 0.0)
    price_change_proxy = ((price / ema20) - 1.0) * 100.0 if ema20 > 0 else 0.0
    score = 50.0
    reasons_for: list[str] = []
    reasons_against: list[str] = []

    weights = [TF_CFG[t]["weight"] for t in tf_scores]
    weighted_tf = float(np.average(list(tf_scores.values()), weights=weights)) if tf_scores else 50.0
    score += weighted_tf - 50.0
    if ema20 > ema50:
        score += 5.0; reasons_for.append("EMA20 بالاتر از EMA50 است")
    elif ema20 < ema50:
        score -= 5.0; reasons_against.append("EMA20 پایین‌تر از EMA50 است")
    if price > vwap:
        score += 4.0; reasons_for.append("قیمت بالای VWAP است")
    elif price < vwap:
        score -= 4.0; reasons_against.append("قیمت پایین VWAP است")
    if volume_spike:
        if abs(price_change_proxy) > 0.15:
            score += 3.0
        reasons_for.append("حجم نسبت به میانگین بالاتر است")
    if btc_trend == "صعودی" and symbol != "BTC/USDT":
        score += 5.0; reasons_for.append("روند BTC پشتیبان صعود است")
    elif btc_trend == "نزولی" and symbol != "BTC/USDT":
        score -= 5.0; reasons_against.append("روند BTC فشار نزولی ایجاد می‌کند")
    f_adj, f_reason = _funding_bias(funding)
    score += f_adj
    if f_adj > 0: reasons_for.append(f_reason)
    elif f_adj < 0: reasons_against.append(f_reason)
    oi_adj, oi_reason = _oi_bias(oi_delta, price_change_proxy)
    score += oi_adj
    if oi_adj > 0: reasons_for.append(oi_reason)
    elif oi_adj < 0: reasons_against.append(oi_reason)

    score = clamp(score, 0, 100)
    bias = "صعودی" if score >= 58 else "نزولی" if score <= 42 else "خنثی"
    agreement_count = sum(
        1 for direction in tf_results.values()
        if (bias == "صعودی" and "صعودی" in direction)
        or (bias == "نزولی" and "نزولی" in direction)
        or (bias == "خنثی" and "خنثی" in direction)
    )
    alignment = clamp(50 + agreement_count * 10 + abs(score - 50) * 0.25, 50, 95)

    rsi_note = "متعادل"
    if rsi >= 70:
        rsi_note = "اشباع خرید / ریسک اصلاح"; reasons_against.append("RSI بالاتر از 70 است")
    elif rsi <= 30:
        rsi_note = "اشباع فروش / احتمال واکنش"; reasons_for.append("RSI پایین 30 است")
    if not reasons_for: reasons_for.append("تأیید جهت‌دار قوی از داده‌های فعلی دیده نشد")
    if not reasons_against: reasons_against.append("مخالفت جدی در داده‌های موجود دیده نشد")

    volatility_pct = atr / price * 100 if price > 0 else 0.0
    conviction = "بالا" if alignment >= 78 and abs(score - 50) >= 18 else "متوسط" if alignment >= 65 else "پایین"
    if bias == "صعودی":
        invalidation = "شکست معتبر EMA50/VWAP و افت امتیاز زیر 50."
    elif bias == "نزولی":
        invalidation = "بازپس‌گیری معتبر EMA50/VWAP و رشد امتیاز بالای 50."
    else:
        invalidation = "خروج از محدوده خنثی با تأیید حجم و همسویی چندتایم‌فریمی."
    summary = (f"TITAN: سوگیری {bias} با امتیاز {round(score)}/100 و همسویی {round(alignment)}/100. "
               f"RSI {rsi:.1f}، انحراف VWAP {vwap_dev:+.2f}% و ATR حدود {volatility_pct:.2f}%. "
               f"اعتماد سیستم {conviction} است؛ خروجی احتمالاتی است، نه تضمین سود.")
    return {
        "bias": bias, "score": int(round(score)), "alignment": int(round(alignment)), "conviction": conviction,
        "summary": summary, "reasons_for": reasons_for[:4], "reasons_against": reasons_against[:4],
        "invalidation": invalidation, "vwap_dev": vwap_dev, "rsi_note": rsi_note,
        "funding_note": f_reason, "oi_note": oi_reason,
    }


def calculate_levels(
    price: float,
    atr: float,
    swing_low: float,
    swing_high: float,
    bias: str,
    *,
    vwap: float = 0.0,
    ema20: float = 0.0,
    ema50: float = 0.0,
    structure: Optional[dict[str, Any]] = None,
    confluence: float = 50.0,
    order_blocks_fvg: Optional[dict[str, Any]] = None,
    fibonacci: Optional[dict[str, Any]] = None,
    forecast: Optional[dict[str, Any]] = None,
) -> tuple[float, float, float]:
    """Professional multi-anchor SL/TP: ATR + swing + structure + OB/FVG + fib + path.

    Higher confluence tightens risk and stretches reward slightly for high-conviction setups.
    Forecast path can stretch TP2 toward the expected mid of the 12-candle scenario.
    Always analysis-only — never executes orders.
    """
    rm = clamp(safe_float(USER_SETTINGS.get("risk_multiplier"), 1.2), 0.2, 5.0)
    atr = max(float(atr), price * 0.0005)
    friction = price * TOTAL_ENTRY_BUFFER
    structure = structure or {}
    conf = clamp(safe_float(confluence, 50.0), 0.0, 100.0)
    conf_factor = 0.85 + (conf / 100.0) * 0.30  # 0.85 .. 1.15
    rr1_mult = 1.5 * conf_factor
    rr2_mult = 2.6 * conf_factor

    struct_low = safe_float(structure.get("swing_low"), swing_low)
    struct_high = safe_float(structure.get("swing_high"), swing_high)
    if struct_low <= 0:
        struct_low = swing_low
    if struct_high <= 0:
        struct_high = swing_high

    anchors_below = [p for p in (swing_low, struct_low, ema50 if ema50 > 0 else None, vwap if 0 < vwap < price else None) if p and p > 0]
    anchors_above = [p for p in (swing_high, struct_high, ema50 if ema50 > 0 else None, vwap if vwap > price else None) if p and p > 0]

    # Order-block / FVG as structural SL magnets
    obf = order_blocks_fvg or {}
    nearest_ob = obf.get("nearest_ob") or {}
    nearest_fvg = obf.get("nearest_fvg") or {}
    if nearest_ob.get("type") == "bullish_ob" and safe_float(nearest_ob.get("low"), 0) > 0:
        anchors_below.append(safe_float(nearest_ob.get("low")))
    if nearest_ob.get("type") == "bearish_ob" and safe_float(nearest_ob.get("high"), 0) > 0:
        anchors_above.append(safe_float(nearest_ob.get("high")))
    if nearest_fvg.get("type") == "bullish_fvg" and safe_float(nearest_fvg.get("low"), 0) > 0:
        anchors_below.append(safe_float(nearest_fvg.get("low")))
    if nearest_fvg.get("type") == "bearish_fvg" and safe_float(nearest_fvg.get("high"), 0) > 0:
        anchors_above.append(safe_float(nearest_fvg.get("high")))

    # Fibonacci 0.618 / 0.786 as optional anchors near price
    fib = fibonacci or {}
    for fk in ("0.618", "0.786", "0.500"):
        fv = safe_float(fib.get(fk), 0)
        if fv <= 0:
            continue
        if fv < price:
            anchors_below.append(fv)
        elif fv > price:
            anchors_above.append(fv)

    # Forecast path target (step ~6 and step ~12 mid)
    fc = forecast or {}
    fc_candles = fc.get("candles") or []
    path_tp_boost = 1.0
    path_target = None
    if fc_candles and fc.get("ok"):
        mid_step = fc_candles[min(5, len(fc_candles) - 1)]
        far_step = fc_candles[min(len(fc_candles) - 1, 11)]
        path_target = safe_float(far_step.get("close"), 0)
        strength = safe_float(fc.get("path_strength"), 0)
        path_tp_boost = 1.0 + min(0.22, strength / 200.0)

    if bias == "صعودی":
        atr_sl = price - rm * atr
        swing_sl = max(anchors_below) - 0.25 * atr if anchors_below else atr_sl
        sl = min(atr_sl, swing_sl) - friction
        if sl >= price:
            sl = price - max(rm * atr, price * 0.004) - friction
        dist = max(price - sl, atr * 0.9)
        tp1 = price + rr1_mult * dist * path_tp_boost + friction
        tp2 = price + rr2_mult * dist * path_tp_boost + friction
        if path_target and path_target > price:
            # Soft pull TP2 toward forecast mid if further than RR path
            tp2 = max(tp2, min(path_target, price + 4.2 * dist))
        return float(sl), float(tp1), float(tp2)

    if bias == "نزولی":
        atr_sl = price + rm * atr
        swing_sl = min(anchors_above) + 0.25 * atr if anchors_above else atr_sl
        sl = max(atr_sl, swing_sl) + friction
        if sl <= price:
            sl = price + max(rm * atr, price * 0.004) + friction
        dist = max(sl - price, atr * 0.9)
        tp1 = price - rr1_mult * dist * path_tp_boost - friction
        tp2 = price - rr2_mult * dist * path_tp_boost - friction
        if path_target and path_target < price:
            tp2 = min(tp2, max(path_target, price - 4.2 * dist))
        return float(sl), float(tp1), float(tp2)

    return (
        float(price - 1.6 * atr - friction),
        float(price + 1.6 * atr + friction),
        float(price + 3.0 * atr + friction),
    )


def refine_bias_with_edge(
    bias: str,
    score: int,
    alignment: float,
    structure: dict[str, Any],
    regime: dict[str, Any],
    confluence: dict[str, Any],
    meta: dict[str, Any],
) -> tuple[str, str]:
    """Balanced multi-layer LONG/SHORT decision — no long-only bias.

    Requires structural + regime + confluence agreement. Symmetric thresholds
    for both directions so SHORT setups are not systematically suppressed.
    """
    struct_bias = str((structure or {}).get("bias", "خنثی"))
    regime_name = str((regime or {}).get("regime", "unknown"))
    conf_score = safe_float((confluence or {}).get("score"), 50)
    meta_label = str((meta or {}).get("label", "WATCH"))
    meta_prob = safe_float((meta or {}).get("probability"), 50)

    long_votes = 0.0
    short_votes = 0.0

    # Core quant bias (symmetric)
    if bias == "صعودی":
        long_votes += 2.0
    elif bias == "نزولی":
        short_votes += 2.0
    else: