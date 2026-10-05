                    self.state["error_patterns"]["ignored_ai_long"] = int(self.state["error_patterns"].get("ignored_ai_long", 0)) + 1
        bucket = self.state["symbols"].setdefault(
            symbol, {"long_wins": 0, "long_losses": 0, "short_wins": 0, "short_losses": 0}
        )
        if is_long:
            bucket["long_wins" if win else "long_losses"] += 1
        elif is_short:
            bucket["short_wins" if win else "short_losses"] += 1
        self._rebalance_weights()
        self._save()

    def _rebalance_weights(self) -> None:
        """Self-tune layer weights and thresholds from observed WIN/LOSS patterns."""
        g = self.state["global"]
        long_wr = self._wr(g["long_wins"], g["long_losses"])
        short_wr = self._wr(g["short_wins"], g["short_losses"])
        agree_n = g["ai_agree_wins"] + g["ai_agree_losses"]
        disagree_n = g["ai_disagree_wins"] + g["ai_disagree_losses"]
        agree_wr = self._wr(g["ai_agree_wins"], g["ai_agree_losses"])
        disagree_wr = self._wr(g["ai_disagree_wins"], g["ai_disagree_losses"])
        # Recompute from stable defaults; do not ratchet thresholds/weights merely because
        # this method is called repeatedly with the same observations.
        defaults = self._default()
        w = dict(defaults["weights"])
        th = dict(defaults["thresholds"])
        ep = self.state["error_patterns"]

        # When trades that agreed with AI win more → increase AI weight
        if agree_n >= 5 and agree_wr >= disagree_wr + 6:
            w["ai"] = min(0.38, float(w.get("ai", 0.26)) + 0.025)
            w["quant"] = max(0.16, float(w.get("quant", 0.26)) - 0.015)
            w["confluence"] = min(0.18, float(w.get("confluence", 0.14)) + 0.005)
        elif disagree_n >= 5 and disagree_wr > agree_wr + 8:
            # Rare: quant alone better — small quant bump but keep AI gate
            w["quant"] = min(0.32, float(w.get("quant", 0.26)) + 0.015)
            w["ai"] = max(0.18, float(w.get("ai", 0.26)) - 0.01)

        # False long epidemic → harder long threshold, more structure weight
        if ep.get("false_long", 0) >= 4 and long_wr < 48:
            th["long_score"] = min(70.0, float(th.get("long_score", 58)) + 1.2)
            th["min_confluence"] = min(78.0, float(th.get("min_confluence", 62)) + 1.0)
            w["structure"] = min(0.24, float(w.get("structure", 0.16)) + 0.01)
            w["quant"] = max(0.15, float(w.get("quant", 0.26)) - 0.01)
        if ep.get("false_short", 0) >= 4 and short_wr < 48:
            th["short_score"] = max(30.0, float(th.get("short_score", 42)) - 1.2)
            th["min_confluence"] = min(78.0, float(th.get("min_confluence", 62)) + 1.0)
            w["structure"] = min(0.24, float(w.get("structure", 0.16)) + 0.01)

        # Good side performance → slightly easier barriers
        if (g["long_wins"] + g["long_losses"]) >= 10 and long_wr >= 58:
            th["long_score"] = max(54.0, float(th.get("long_score", 58)) - 0.6)
        if (g["short_wins"] + g["short_losses"]) >= 10 and short_wr >= 58:
            th["short_score"] = min(46.0, float(th.get("short_score", 42)) + 0.6)

        # Normalize weights
        total = sum(float(v) for v in w.values()) or 1.0
        self.state["weights"] = {k: round(float(v) / total, 4) for k, v in w.items()}
        self.state["thresholds"] = th

    def side_reliability(self, symbol: str = "") -> dict[str, float]:
        g = self.state["global"]
        long_wr = self._wr(g["long_wins"], g["long_losses"])
        short_wr = self._wr(g["short_wins"], g["short_losses"])
        sym = self.state["symbols"].get(symbol) or {}
        if (sym.get("long_wins", 0) + sym.get("long_losses", 0)) >= 4:
            long_wr = 0.55 * long_wr + 0.45 * self._wr(sym["long_wins"], sym["long_losses"])
        if (sym.get("short_wins", 0) + sym.get("short_losses", 0)) >= 4:
            short_wr = 0.55 * short_wr + 0.45 * self._wr(sym["short_wins"], sym["short_losses"])
        return {
            "long_wr": round(long_wr, 2),
            "short_wr": round(short_wr, 2),
            "long_samples": int(g["long_wins"] + g["long_losses"]),
            "short_samples": int(g["short_wins"] + g["short_losses"]),
        }

    def fuse_decision(
        self,
        *,
        symbol: str,
        quant_bias: str,
        score: float,
        alignment: float,
        structure: dict[str, Any],
        regime: dict[str, Any],
        confluence: dict[str, Any],
        meta: dict[str, Any],
        ai_ensemble: dict[str, Any],
        forecast: Optional[dict[str, Any]] = None,
        patterns: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Professional multi-layer fusion — TITAN ↔ AI ↔ forecast ↔ patterns.

        Rules (priority):
        1) Strong AI↔quant disagreement → WAIT (do not force a side)
        2) AI consensus can promote borderline quant only with structure/confluence support
        3) Historical false-long / false-short raises the bar for that side
        4) Success probability must clear a floor before LONG/SHORT leaves WAIT
        5) Weights self-tune from past WIN/LOSS via _rebalance_weights
        6) 12-candle forecast path and chart patterns vote as soft directional layers
        """
        w = dict(self.state.get("weights") or {})
        th = dict(self.state.get("thresholds") or {})
        # Ensure weight keys exist and normalize
        for k, default in (("quant", 0.26), ("structure", 0.16), ("regime", 0.12),
                           ("confluence", 0.14), ("ai", 0.26), ("history", 0.06)):
            w[k] = float(w.get(k, default))
        ssum = sum(w.values()) or 1.0
        w = {k: v / ssum for k, v in w.items()}

        rel = self.side_reliability(symbol)
        struct_bias = str((structure or {}).get("bias", "خنثی"))
        regime_name = str((regime or {}).get("regime", "unknown"))
        conf = safe_float((confluence or {}).get("score"), 50)
        meta_prob = safe_float((meta or {}).get("probability"), 50)
        meta_label = str((meta or {}).get("label", "WATCH"))
        ai_majority = str((ai_ensemble or {}).get("majority", "WAIT"))
        ai_target_agree = safe_float((ai_ensemble or {}).get("agreement"), 0)
        ai_agree = safe_float((ai_ensemble or {}).get("majority_agreement"), ai_target_agree)
        ai_status = str((ai_ensemble or {}).get("status", "NO_AI_DATA"))
        ai_providers = int((ai_ensemble or {}).get("providers", 0) or 0)
        tally = (ai_ensemble or {}).get("tally") or {}
        errors = self.state.get("error_patterns") or {}

        def dir_score(label: str) -> float:
            lab = str(label or "")
            if lab in {"صعودی", "LONG", "trend_up", "BOS_UP", "BULL_STRUCTURE"} or "up" in lab.lower():
                return 1.0
            if lab in {"نزولی", "SHORT", "trend_down", "BOS_DOWN", "BEAR_STRUCTURE"} or "down" in lab.lower():
                return -1.0
            return 0.0

        quant_cont = clamp((float(score) - 50.0) / 50.0, -1.0, 1.0)
        quant_d = dir_score(quant_bias) if quant_bias != "خنثی" else quant_cont * 0.5
        struct_d = dir_score(struct_bias)
        regime_d = dir_score(regime_name)
        conf_d = quant_cont if conf >= 58 else quant_cont * 0.35
        ai_d = dir_score(ai_majority)

        hist_d = 0.0
        if rel["long_wr"] >= rel["short_wr"] + 8 and rel["long_samples"] >= 5:
            hist_d = 0.4
        elif rel["short_wr"] >= rel["long_wr"] + 8 and rel["short_samples"] >= 5:
            hist_d = -0.4
        elif rel["long_wr"] < 42 and rel["long_samples"] >= 8:
            hist_d = -0.25  # punish historically bad longs
        elif rel["short_wr"] < 42 and rel["short_samples"] >= 8:
            hist_d = 0.25

        # Forecast + pattern soft votes (do not dominate quant/AI)
        fc = forecast or {}
        fc_d = dir_score(str(fc.get("overall_bias") or ""))
        _fc_acc = forecast_path_accuracy_stats(symbol)
        _fc_scale = safe_float(_fc_acc.get("weight_scale"), 0.35)
        fc_w = (min(0.12, 0.04 + safe_float(fc.get("path_strength"), 0) / 400.0) * _fc_scale) if fc.get("ok") else 0.0
        pat = patterns or {}
        primary = pat.get("primary") or {}
        pat_d = dir_score(str((primary.get("guide") or {}).get("bias") or ""))
        pat_conf = safe_float(primary.get("confidence"), 0) / 100.0
        pat_w = min(0.10, 0.03 + pat_conf * 0.07) if primary else 0.0

        # Renormalize so total weight stays 1.0 after adding soft layers
        base_keys = ("quant", "structure", "regime", "confluence", "ai", "history")
        base_sum = sum(w[k] for k in base_keys) or 1.0
        soft = fc_w + pat_w
        scale = max(0.78, 1.0 - soft)
        for k in base_keys:
            w[k] = w[k] / base_sum * scale
        w["forecast"] = fc_w
        w["pattern"] = pat_w

        fused = (
            w["quant"] * quant_d
            + w["structure"] * struct_d
            + w["regime"] * regime_d
            + w["confluence"] * conf_d
            + w["ai"] * ai_d
            + w["history"] * hist_d
            + w["forecast"] * fc_d
            + w["pattern"] * pat_d
        )
        fused = float(clamp(fused, -1.0, 1.0))
        if fc_d != 0 and fc.get("ok"):
            # mild agreement note later via explanation
            pass

        # Adaptive barriers from learned thresholds + error patterns
        long_barrier = float(th.get("long_score", 58))
        short_barrier = float(th.get("short_score", 42))
        min_align = float(th.get("min_alignment", 58))
        min_conf = float(th.get("min_confluence", 62))
        ai_need = float(th.get("ai_consensus_pct", 55))
        promote_buf = float(th.get("promote_score_buffer", 4))

        # Raise bar if system keeps making false longs/shorts
        fl = int(errors.get("false_long", 0) or 0)
        fs = int(errors.get("false_short", 0) or 0)
        if fl >= 5:
            long_barrier = min(70.0, long_barrier + min(6.0, fl * 0.35))
        if fs >= 5:
            short_barrier = max(30.0, short_barrier - min(6.0, fs * 0.35))

        # Symbol-specific reliability adjusts barriers
        if rel["long_samples"] >= 6 and rel["long_wr"] < 45:
            long_barrier = min(72.0, long_barrier + 3)
        if rel["short_samples"] >= 6 and rel["short_wr"] < 45:
            short_barrier = max(28.0, short_barrier - 3)
        if rel["long_samples"] >= 6 and rel["long_wr"] > 60:
            long_barrier = max(54.0, long_barrier - 2)
        if rel["short_samples"] >= 6 and rel["short_wr"] > 60:
            short_barrier = min(46.0, short_barrier + 2)

        explanation: list[str] = []
        explanation.append(f"هم‌جوشی={fused:+.2f} · وزن AI={w['ai']:.0%} · وزن کمی={w['quant']:.0%}")

        # --- AI alignment gates ---
        quant_side = "LONG" if quant_bias == "صعودی" or score >= 54 else "SHORT" if quant_bias == "نزولی" or score <= 46 else "WAIT"
        ai_conflict = False
        if ai_providers >= 1 and ai_majority in {"LONG", "SHORT"} and quant_side in {"LONG", "SHORT"}:
            if ai_majority != quant_side:
                ai_conflict = True
                explanation.append(f"اختلاف TITAN({quant_side}) با اکثریت هوش‌مصنوعی({ai_majority})")

        # Base technical OK flags (balanced: real edge can pass without extreme fusion)
        long_ok = (
            fused >= 0.10
            and score >= long_barrier
            and alignment >= min_align - 4
            and conf >= min_conf - 12
            and quant_d >= -0.22
        )
        short_ok = (
            fused <= -0.10
            and score <= short_barrier
            and alignment >= min_align - 4
            and conf >= min_conf - 12
            and quant_d <= 0.22
        )

        # Graded AI interaction: AI is a decision partner, not an automatic veto.
        # A single disagreement must not erase a technically strong opportunity.
        # Only a strong AI consensus against a weak quant setup is allowed to veto.
        if ai_conflict and ai_providers >= 1:
            strong_quant_long = quant_side == "LONG" and fused >= 0.30 and conf >= 68 and struct_d >= 0
            strong_quant_short = quant_side == "SHORT" and fused <= -0.30 and conf >= 68 and struct_d <= 0
            hard_ai_veto = ai_agree >= 78 and fused < 0.22 if quant_side == "LONG" else ai_agree >= 78 and fused > -0.22 if quant_side == "SHORT" else False
            if hard_ai_veto and not (strong_quant_long or strong_quant_short):
                if quant_side == "LONG": long_ok = False
                if quant_side == "SHORT": short_ok = False
                explanation.append("اجماع قوی AI خلاف setup ضعیف → عدم ورود")
            else:
                # Keep the quant opportunity alive, but reduce conviction slightly.
                explanation.append("اختلاف AI/TITAN → رأی AI به‌عنوان جریمه وزن‌دار لحاظ شد، نه وتوی کامل")

        # AI majority veto of weak quant
        if ai_providers >= 1 and ai_majority == "SHORT" and ai_agree >= ai_need:
            if long_ok and fused < 0.40:
                long_ok = False
                explanation.append("وتوی فروش هوش مصنوعی روی خرید ضعیف")
        if ai_providers >= 1 and ai_majority == "LONG" and ai_agree >= ai_need:
            if short_ok and fused > -0.40:
                short_ok = False
                explanation.append("وتوی خرید هوش مصنوعی روی فروش ضعیف")

        # AI promotion of borderline setups (only same direction)
        if ai_providers >= 1 and ai_agree >= ai_need:
            if ai_majority == "LONG" and not long_ok:
                if (
                    score >= long_barrier - max(5.0, promote_buf)
                    and alignment >= min_align - 7
                    and fused >= 0.08
                    and struct_d >= -0.10
                    and conf >= 53
                ):
                    long_ok = True
                    explanation.append("ارتقای مرزی خرید با اجماع هوش مصنوعی")
            if ai_majority == "SHORT" and not short_ok:
                if (
                    score <= short_barrier + max(5.0, promote_buf)
                    and alignment >= min_align - 7
                    and fused <= -0.08
                    and struct_d <= 0.10
                    and conf >= 53
                ):
                    short_ok = True
                    explanation.append("ارتقای مرزی فروش با اجماع هوش مصنوعی")

        # Meta-label gate
        if meta_label == "REJECT":
            long_ok = False
            short_ok = False
            explanation.append("متا-برچسب رد → انتظار")

        # Success probability model (0-100)
        base_succ = 50.0
        base_succ += abs(fused) * 22.0
        base_succ += (alignment - 50) * 0.25
        base_succ += (conf - 50) * 0.20
        base_succ += (meta_prob - 50) * 0.15
        if ai_providers >= 1:
            if not ai_conflict:
                base_succ += min(12.0, ai_agree * 0.12)
            else:
                base_succ -= min(15.0, 8 + (100 - ai_agree) * 0.05)
        # Historical calibration is sample-size weighted and Bayesian-shrunk.
        # `success_probability` remains an estimate, not a guaranteed/calibrated market probability.
        side_wr = 50.0; side_n = 0
        if long_ok:
            side_wr, side_n = rel["long_wr"], rel["long_samples"]
        elif short_ok:
            side_wr, side_n = rel["short_wr"], rel["short_samples"]
        hist_weight = min(0.35, max(0.0, side_n) / 60.0 * 0.35)
        base_succ = (1.0 - hist_weight) * base_succ + hist_weight * side_wr
        certainty_cap = 72.0 if side_n < 8 else 80.0 if side_n < 20 else 88.0
        if ai_providers == 0:
            certainty_cap = min(certainty_cap, 76.0)
        success_probability = float(clamp(base_succ, 5, certainty_cap))
        cal_dir = "LONG" if long_ok else "SHORT" if short_ok else ""
        calibration = _probability_calibration(symbol, cal_dir, success_probability)
        calibrated_probability = float(calibration.get("calibrated", success_probability))
        # Blend only partially so sparse historical data cannot dominate the live model.
        if calibration.get("samples", 0) >= 12:
            success_probability = float(clamp(0.55 * success_probability + 0.45 * calibrated_probability, 5, certainty_cap))

        # Hard floor: no directional call without enough estimated success score
        # Opportunity-aware floor: strong multi-factor technical setups can fire even
        # when AI confidence is imperfect. This avoids an always-WAIT system while
        # keeping a minimum quality floor.
        technical_edge = abs(fused) >= 0.28 and alignment >= (min_align - 4) and conf >= (min_conf - 5)
        min_succ = 54.0 if technical_edge else (56.0 if ai_providers >= 1 else 53.0)
        if success_probability < min_succ and not (technical_edge and success_probability >= 52.0):
            if long_ok or short_ok:
                explanation.append(f"احتمال موفقیت {success_probability:.0f}% زیر کف {min_succ:.0f} → انتظار")
            long_ok = False
            short_ok = False

        if long_ok and short_ok:
            decision, final_bias = "WAIT", "خنثی"
            explanation.append("تعارض دوطرفه → انتظار")
        elif long_ok:
            decision, final_bias = "LONG", "صعودی"
            explanation.append(f"تأیید خرید · احتمال≈{success_probability:.0f}%")
        elif short_ok:
            decision, final_bias = "SHORT", "نزولی"
            explanation.append(f"تأیید فروش · احتمال≈{success_probability:.0f}%")
        else:
            decision, final_bias = "WAIT", "خنثی"
            explanation.append("شرایط برای سیگنال جهتی کافی نیست")

        if fc.get("ok") and fc_d != 0:
            agree = (decision == "LONG" and fc_d > 0) or (decision == "SHORT" and fc_d < 0)
            explanation.append(
                f"مسیر {fc.get('horizon', 12)} کندلی: {fc.get('overall_bias')} · "
                f"{'هم‌راستا' if agree else 'ناهم‌راستا'} با تصمیم · قدرت مسیر {safe_float(fc.get('path_strength'), 0):.0f}"
            )
        if primary:
            explanation.append(
                f"الگوی «{primary.get('id')}» (اطمینان {safe_float(primary.get('confidence'), 0):.0f}%) · سوگیری {(primary.get('guide') or {}).get('bias', '—')}"
            )

        return {
            "decision": decision,
            "bias": final_bias,
            "fused_score": round(fused, 4),
            "success_probability": round(success_probability, 1),
            "probability_calibration": calibration,
            "weights": {k: round(v, 4) for k, v in w.items()},
            "thresholds": {
                "long_score": long_barrier,
                "short_score": short_barrier,
                "min_alignment": min_align,
                "min_confluence": min_conf,
                "min_success": min_succ,
            },
            "reliability": rel,
            "ai_majority": ai_majority,
            "ai_agreement": ai_agree,
            "ai_target_agreement": ai_target_agree,
            "ai_conflict": ai_conflict,
            "explanation": explanation,
            "tally": tally,
            "quant_side": quant_side,
        }



TITAN_ADAPTIVE = TitanAdaptiveIntelligence()
try:
    TITAN_ADAPTIVE.refresh_from_db()
except Exception:
    _swallow()



TITAN_EDGE_SUITE = TitanProfessionalEdgeSuite()

def _first_touch_ohlc(window: pd.DataFrame, direction: str, sl: float, tp1: float, tp2: float | None = None) -> tuple[str, str, float | None]:
    """Chronological barrier evaluator using OHLC (vectorised V53.1).

    If SL and TP are both inside the same candle, ordering is unknowable at that
    granularity, so the result is AMBIGUOUS rather than silently choosing a winner.
    """
    if window is None or window.empty or not np.isfinite(sl) or not np.isfinite(tp1):
        return "MISS", "NONE", None
    is_long = direction in {"صعودی", "LONG", "long"}
    is_short = direction in {"نزولی", "SHORT", "short"}
    if not (is_long or is_short):
        return "NEUTRAL", "NONE", None
    w = window.sort_values("t", kind="stable")
    hi = _fast_col(w, "high", np.nan); lo = _fast_col(w, "low", np.nan)
    valid = np.isfinite(hi) & np.isfinite(lo)
    sl_hit = (lo <= sl) if is_long else (hi >= sl)
    tp1_hit = (hi >= tp1) if is_long else (lo <= tp1)
    has_tp2 = tp2 is not None and np.isfinite(tp2)
    tp2_hit = ((hi >= tp2) if is_long else (lo <= tp2)) if has_tp2 else np.zeros_like(sl_hit)
    hit = valid & (sl_hit | tp1_hit | tp2_hit)
    if not hit.any():
        return "MISS", "NONE", None
    i = int(hit.argmax())
    if sl_hit[i] and (tp1_hit[i] or tp2_hit[i]):
        return "AMBIGUOUS", "BOTH_SAME_BAR", None
    if sl_hit[i]:
        return "LOSS", "SL", float(sl)
    if tp2_hit[i]:
        return "WIN", "TP2", float(tp2)
    return "WIN", "TP1", float(tp1)


# ============================================================
# PAPER / FORECAST / METRICS
# ============================================================


def _record_setup_outcome(item: dict[str, Any], pnl_r: float) -> None:
    key = "|".join([str(item.get("bias")), str(item.get("signal_tag")), str(item.get("tfs", {}).get("1h", "")), str(item.get("tfs", {}).get("4h", ""))])
    with DB_LOCK, db_conn() as con:
        row = con.execute("SELECT trades,wins,losses,pnl_r FROM setup_stats WHERE setup_key=?", (key,)).fetchone()
        trades, wins, losses, pnl = tuple(row) if row else (0, 0, 0, 0.0)
        trades += 1
        wins += int(pnl_r > 0); losses += int(pnl_r < 0); pnl += pnl_r
        con.execute("INSERT OR REPLACE INTO setup_stats(setup_key,trades,wins,losses,pnl_r,updated_at) VALUES(?,?,?,?,?,?)", (key, trades, wins, losses, pnl, time.time()))


def calculate_position_size(account_size: float, risk_pct: float, entry: float, stop: float) -> dict[str, float]:
    account = max(0.0, float(account_size)); risk = clamp(float(risk_pct), 0.01, 20.0); distance = abs(entry - stop)
    risk_cash = account * risk / 100.0; quantity = risk_cash / distance if distance > 0 else 0.0
    return {"risk_cash": round(risk_cash, 4), "quantity": round(quantity, 8), "notional": round(quantity * max(entry, 0), 4)}


def paper_open_signal(item: dict[str, Any], account_size: float = 10000.0, risk_pct: float = 1.0) -> bool:
    """Open paper only with valid oriented levels — never store tp=0 or inverted SL/TP."""
    try:
        entry = safe_float(str(item.get("price", "0")).replace(",", ""))
        sl = safe_float(str(item.get("stop_loss", "0")).replace(",", ""))
        tp1 = safe_float(str(item.get("tp1", "0")).replace(",", ""))
        tp2 = safe_float(str(item.get("tp2", "0")).replace(",", ""))
        bias = str(item.get("bias") or "")
        decision = str(item.get("decision_tag") or item.get("decision") or "")
        if decision in {"LONG", "SHORT"}:
            side = decision
        elif bias in {"صعودی", "LONG"}:
            side = "LONG"
        elif bias in {"نزولی", "SHORT"}:
            side = "SHORT"
        else:
            return False
        if entry <= 0 or sl <= 0 or tp1 <= 0:
            return False
        if side == "LONG" and not (sl < entry < tp1):
            return False
        if side == "SHORT" and not (tp1 < entry < sl):
            return False
        risk = abs(entry - sl)
        if risk <= entry * 0.0005 or abs(tp1 - entry) / risk < MIN_EFFECTIVE_RR:
            return False
        size = calculate_position_size(account_size, risk_pct, entry, sl)
        pred_prob = float(clamp(safe_float(
            item.get("success_probability", item.get("success_prob", item.get("decision_confidence", 50.0))), 50.0
        ), 1.0, 85.0))
        with DB_LOCK, db_conn() as con:
            if con.execute("SELECT 1 FROM paper_trades WHERE symbol=? AND status='OPEN' LIMIT 1", (item["symbol"],)).fetchone():
                return False
            if con.execute("SELECT 1 FROM paper_trades WHERE symbol=? AND created_at>=? LIMIT 1",
                           (item["symbol"], time.time() - SIGNAL_COOLDOWN_SECONDS)).fetchone():
                return False
            con.execute(
                "INSERT INTO paper_trades(symbol,timeframe,created_at,decision,entry,sl,tp1,tp2,quantity,risk_pct,status,success_prob) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (item["symbol"], "1h", time.time(), side, entry, sl, tp1, tp2 if tp2 > 0 else tp1,
                 size["quantity"], risk_pct, "OPEN", pred_prob),
            )
        return True
    except Exception as exc:
        LOGGER.warning("Paper signal failed: %s", exc)
        return False


def evaluate_paper_trades() -> None:
    """Chronological first-touch; guards against exit_price=0 and invalid levels."""
    now = time.time()
    with DB_LOCK, db_conn() as con:
        rows = con.execute("SELECT * FROM paper_trades WHERE status='OPEN' ORDER BY created_at LIMIT 200").fetchall()
    for row in rows:
        try:
            entry = safe_float(row["entry"], 0)
            sl = safe_float(row["sl"], np.nan)
            tp1 = safe_float(row["tp1"], np.nan)
            tp2 = safe_float(row["tp2"], np.nan)
            decision = str(row["decision"] or "")
            if entry <= 0 or not (math.isfinite(sl) and sl > 0) or not (math.isfinite(tp1) and tp1 > 0):
                with DB_LOCK, db_conn() as con:
                    con.execute(
                        "UPDATE paper_trades SET status='CLOSED',exit_price=?,pnl_pct=0,r_multiple=0,closed_at=?,reason=? WHERE id=?",
                        (entry, now, "INVALID_LEVELS", row["id"]),
                    )
                continue
            start_ms = int(float(row["created_at"]) * 1000)
            df = fetch_klines(row["symbol"], "15m", 1000, start_ms=start_ms)
            window = df[df["t"] >= start_ms]
            outcome, hit, exit_price = _first_touch_ohlc(window, decision, sl, tp1, tp2)
            if outcome not in {"WIN", "LOSS"}:
                created = safe_float(row["created_at"], now)
                if now >= created + 240 * 60 and not window.empty:
                    outcome, hit = "TIME_EXIT", "TIME_EXIT"
                    exit_price = safe_float(window["close"].iloc[-1], entry)
                else:
                    continue
            exit_price = safe_float(exit_price, 0.0)
            if exit_price <= 0 or not math.isfinite(exit_price):
                exit_price = safe_float(window["close"].iloc[-1], entry) if not window.empty else entry
                hit = "INVALID_EXIT_RECOVERED"
            pnl_pct = ((exit_price - entry) / entry) * 100.0 if entry else 0.0
            if decision in {"نزولی", "SHORT"}:
                pnl_pct *= -1
            pnl_pct -= TOTAL_ENTRY_BUFFER * 2.0 * 100.0
            risk_per_unit = abs(entry - sl)
            if risk_per_unit <= 0:
                r_multiple = 0.0
            elif decision in {"صعودی", "LONG"}:
                r_multiple = (exit_price - entry) / risk_per_unit
            else:
                r_multiple = (entry - exit_price) / risk_per_unit
            r_multiple -= (TOTAL_ENTRY_BUFFER * 2.0 * entry / risk_per_unit) if risk_per_unit else 0.0
            r_multiple = float(clamp(r_multiple, -5.0, 5.0))
            pnl_pct = float(clamp(pnl_pct, -25.0, 25.0))
            with DB_LOCK, db_conn() as con:
                con.execute(
                    "UPDATE paper_trades SET status='CLOSED',exit_price=?,pnl_pct=?,r_multiple=?,closed_at=?,reason=? WHERE id=?",
                    (exit_price, pnl_pct, r_multiple, now, hit, row["id"]),
                )
        except Exception as exc:
            LOGGER.warning("Paper evaluation failed #%s: %s", row["id"], exc)

def store_forecasts(market_data: list[dict[str, Any]]) -> None:
    """Store one independent directional forecast per symbol/cooldown window.

    This avoids inflating sample size by inserting essentially the same signal every
    dashboard refresh. WAIT/neutral rows are not used as pseudo-trades.
    """
    now = time.time()
    with DB_LOCK, db_conn() as con:
        for item in market_data:
            decision = str(item.get("decision_tag") or (item.get("edge") or {}).get("decision_tag") or "WAIT")
            direction = "صعودی" if decision == "LONG" else "نزولی" if decision == "SHORT" else "خنثی"
            if decision not in {"LONG", "SHORT"} or safe_float(item.get("signal_quality"), 0) < MIN_DIRECTIONAL_QUALITY:
                continue
            _g = str(item.get("grade") or (item.get("signal_grade") or {}).get("grade") or "")
            if _g in {"D", "F"}:
                continue
            recent = con.execute(
                "SELECT 1 FROM forecasts WHERE symbol=? AND timeframe='1h' AND direction=? AND created_at>=? LIMIT 1",
                (item["symbol"], direction, now - SIGNAL_COOLDOWN_SECONDS),
            ).fetchone()
            if recent:
                continue
            fusion = (item.get("titan_analysis") or {}).get("fusion") or item.get("fusion") or {}
            edge_ai = (item.get("edge") or {}).get("ai") or {}
            ai_maj = str(fusion.get("ai_majority") or edge_ai.get("majority") or "")
            ai_ag = safe_float(fusion.get("ai_agreement") or edge_ai.get("majority_agreement") or edge_ai.get("agreement"), 0)
            fused = safe_float(fusion.get("fused_score"), 0); succ = safe_float(fusion.get("success_probability"), 0)
            forecast_obj = item.get("candle_forecast") or {}
            predicted_move = safe_float(forecast_obj.get("expected_move_pct"), 0.0)
            con.execute(
                "INSERT INTO forecasts(symbol,timeframe,created_at,direction,score,alignment,price,sl,tp1,tp2,horizon_minutes,source,ai_majority,ai_agreement,fused_score,success_prob,predicted_move_pct) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (item["symbol"], "1h", now, direction, item["score"], item["alignment"],
                 safe_float(str(item.get("price", "0")).replace(",", "")),
                 safe_float(str(item.get("stop_loss", "0")).replace(",", "")),
                 safe_float(str(item.get("tp1", "0")).replace(",", "")),
                 safe_float(str(item.get("tp2", "0")).replace(",", "")),
                 240, "quant+ai-fusion-v5-audited", ai_maj, ai_ag, fused, succ, predicted_move),
            )

def evaluate_pending_forecasts() -> None:
    now = time.time()
    with DB_LOCK, db_conn() as con:
        rows = con.execute("SELECT * FROM forecasts WHERE outcome='PENDING' AND created_at <= ? LIMIT 100", (now,)).fetchall()
    learned_any = False
    for row in rows:
        horizon_end = float(row["created_at"]) + int(row["horizon_minutes"]) * 60
        if now < horizon_end:
            continue
        try:
            start_ms = int(float(row["created_at"]) * 1000)
            end_ms = int(horizon_end * 1000)
            bars_needed = max(20, min(1000, int(math.ceil(int(row["horizon_minutes"]) / 15)) + 4))
            df = fetch_klines(row["symbol"], "15m", bars_needed, start_ms=start_ms, end_ms=end_ms)
            window = df[(df["t"] >= start_ms) & (df["t"] <= end_ms)]
            if window.empty:
                continue
            direction = str(row["direction"])
            outcome, hit_type, _ = _first_touch_ohlc(
                window, direction, safe_float(row["sl"], np.nan), safe_float(row["tp1"], np.nan), safe_float(row["tp2"], np.nan)
            )
            with DB_LOCK, db_conn() as con:
                con.execute("UPDATE forecasts SET outcome=?, evaluated_at=?, hit_type=? WHERE id=?", (outcome, now, hit_type, row["id"]))
            learned_any = learned_any or outcome in {"WIN", "LOSS"}
            # Track realized path vs predicted direction for forecast self-weighting
            try:
                entry_px = safe_float(row["price"], 0)
                last_px = float(window["close"].iloc[-1]) if len(window) else 0.0
                if entry_px > 0 and last_px > 0:
                    actual_move = (last_px / entry_px - 1.0) * 100.0
                    pred_bias = str(row["direction"])
                    if pred_bias in {"LONG", "صعودی"}:
                        pred_bias = "صعودی"
                    elif pred_bias in {"SHORT", "نزولی"}:
                        pred_bias = "نزولی"
                    record_forecast_path_outcome(
                        str(row["symbol"]), pred_bias,
                        predicted_move_pct=safe_float(row["predicted_move_pct"], 0.0),
                        actual_move_pct=actual_move,
                    )
            except Exception:
                _swallow()
        except Exception as exc:
            LOGGER.warning("Forecast evaluation failed #%s: %s", row["id"], exc)
    # Rebuild adaptive state from authoritative DB once, preventing duplicate learning.
    if learned_any:
        try:
            TITAN_ADAPTIVE.refresh_from_db()
        except Exception as learn_exc:
            LOGGER.debug("adaptive refresh skip: %s", learn_exc)

def _safe_return_series(values: list[float], return_unit: str = "pct") -> dict[str, Any]:
    arr = np.asarray(values, dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return {"trades": 0, "decisive_trades": 0, "neutral_trades": 0, "win_rate": 0.0, "loss_rate": 0.0,
                "profit_factor": None, "expectancy": 0.0, "max_drawdown": 0.0, "sharpe": 0.0, "sortino": 0.0,
                "avg_win": 0.0, "avg_loss": 0.0, "max_losing_streak": 0, "return_unit": return_unit}
    wins, losses = arr[arr > 0], arr[arr < 0]
    neutral = int((arr == 0).sum()); decisive = int(wins.size + losses.size)
    gross_loss = abs(float(losses.sum())); mean = float(arr.mean())
    account_returns = arr / 100.0 if return_unit == "pct" else arr * 0.01
    account_returns = np.clip(account_returns, -0.999, 10.0)
    equity = np.cumprod(1.0 + account_returns)
    peak = np.maximum.accumulate(equity)
    drawdowns = (peak - equity) / np.maximum(peak, 1e-12) * 100.0
    std = float(arr.std(ddof=1)) if arr.size > 1 else 0.0
    downside = np.minimum(arr, 0.0)
    downside_dev = float(np.sqrt(np.mean(np.square(downside)))) if arr.size else 0.0
    streak = max_streak = 0
    for x in arr:
        if x < 0: streak += 1; max_streak = max(max_streak, streak)
        elif x > 0: streak = 0
    return {
        "trades": int(arr.size), "wins": int(wins.size), "losses": int(losses.size), "decisive_trades": decisive, "neutral_trades": neutral,
        "win_rate": round(wins.size / decisive * 100, 2) if decisive else 0.0,
        "loss_rate": round(losses.size / decisive * 100, 2) if decisive else 0.0,
        "profit_factor": round(float(wins.sum() / gross_loss), 3) if gross_loss > 0 else None,
        "expectancy": round(mean, 4), "max_drawdown": round(float(drawdowns.max()), 4),
        "sharpe": round(mean / std, 3) if std else 0.0,
        "sortino": round(mean / downside_dev, 3) if downside_dev else 0.0,
        "avg_win": round(float(wins.mean()), 4) if wins.size else 0.0,
        "avg_loss": round(float(losses.mean()), 4) if losses.size else 0.0,
        "max_losing_streak": int(max_streak), "return_unit": return_unit,
    }


def get_audit_stats() -> dict[str, Any]:
    """Multi-tier historical accuracy: overall + LONG vs SHORT split + grade."""
    with DB_LOCK, db_conn() as con:
        evaluated = con.execute("SELECT COUNT(*) FROM forecasts WHERE outcome IN ('WIN','LOSS','MISS','NEUTRAL','AMBIGUOUS')").fetchone()[0]
        wins = con.execute("SELECT COUNT(*) FROM forecasts WHERE outcome='WIN'").fetchone()[0]
        losses = con.execute("SELECT COUNT(*) FROM forecasts WHERE outcome='LOSS'").fetchone()[0]
        neutral = con.execute("SELECT COUNT(*) FROM forecasts WHERE outcome IN ('MISS','NEUTRAL')").fetchone()[0]
        ambiguous = con.execute("SELECT COUNT(*) FROM forecasts WHERE outcome='AMBIGUOUS'").fetchone()[0]
        tp_hits = con.execute("SELECT COUNT(*) FROM forecasts WHERE hit_type IN ('TP1','TP2')").fetchone()[0]
        sl_hits = con.execute("SELECT COUNT(*) FROM forecasts WHERE hit_type='SL'").fetchone()[0]
        long_w = con.execute("SELECT COUNT(*) FROM forecasts WHERE outcome='WIN' AND direction IN ('صعودی','LONG')").fetchone()[0]
        long_l = con.execute("SELECT COUNT(*) FROM forecasts WHERE outcome='LOSS' AND direction IN ('صعودی','LONG')").fetchone()[0]
        short_w = con.execute("SELECT COUNT(*) FROM forecasts WHERE outcome='WIN' AND direction IN ('نزولی','SHORT')").fetchone()[0]
        short_l = con.execute("SELECT COUNT(*) FROM forecasts WHERE outcome='LOSS' AND direction IN ('نزولی','SHORT')").fetchone()[0]
    decisive = wins + losses
    wr = round(wins / decisive * 100, 1) if decisive else 0.0
    long_dec = long_w + long_l
    short_dec = short_w + short_l
    long_wr = round(long_w / long_dec * 100, 1) if long_dec else 0.0
    short_wr = round(short_w / short_dec * 100, 1) if short_dec else 0.0
    # Accuracy tier without relying on prior predictions: pure outcome grade
    if decisive < 8:
        tier, tier_label = "C", "نمونه ناکافی"
    elif wr >= 62 and tp_hits >= sl_hits:
        tier, tier_label = "A", "دقت بالا"
    elif wr >= 52:
        tier, tier_label = "B", "دقت قابل قبول"
    elif wr >= 42:
        tier, tier_label = "C", "دقت متوسط"
    else:
        tier, tier_label = "D", "نیاز به بازتنظیم"
    # Wilson lower bound communicates uncertainty better than raw win-rate alone.
    if decisive:
        z = 1.96; phat = wins / decisive
        denom = 1 + z*z/decisive
        centre = phat + z*z/(2*decisive)
        margin = z * math.sqrt((phat*(1-phat) + z*z/(4*decisive))/decisive)
        wr_lower95 = max(0.0, (centre - margin) / denom * 100.0)
    else:
        wr_lower95 = 0.0
    resolution_rate = (decisive / evaluated * 100.0) if evaluated else 0.0
    return {
        "evaluated": int(evaluated), "wins": int(wins), "losses": int(losses),
        "neutral": int(neutral), "ambiguous": int(ambiguous),
        "win_rate": wr, "tp_hits": int(tp_hits), "sl_hits": int(sl_hits),
        "long_win_rate": long_wr, "short_win_rate": short_wr,
        "long_trades": int(long_dec), "short_trades": int(short_dec),
        "win_rate_lower_95": round(wr_lower95, 1), "resolution_rate": round(resolution_rate, 1),
        "tier": tier, "tier_label": tier_label,
        "label": "Historical first-touch audit · deduplicated directional signals · uncertainty-aware",
        "probability_calibration": _probability_calibration(),
    }

# ============================================================
# BACKTEST / WALK FORWARD - FIXED HORIZON LOGIC
# ============================================================





def _probability_calibration(symbol: str = "", direction: str = "", raw_prob: float = 50.0) -> dict[str, Any]:
    """Delegate to Platt+Isotonic calibrator (forecasts + paper trades)."""
    return calibrate_success_probability(symbol=symbol, direction=direction, raw_prob=raw_prob)


def _proxy_trade_candidates(df: pd.DataFrame, long_threshold: float, short_threshold: float,
                            min_gap_long: float, min_gap_short: float, max_momentum_short: float,
                            start: int, end: int, friction: float) -> list[float]:
    out=[]
    for i in range(max(60,start), min(end, len(df)-1)):
        sample=df.iloc[:i+1]
        direction, score, meta=_tf_forecast(sample)
        entry=float(df['close'].iloc[i]); future=float(df['close'].iloc[i+1])
        rsi=safe_float(meta.get('rsi'),50); gap=safe_float(meta.get('ema_gap'),0); mom=safe_float(meta.get('momentum'),0)
        if direction=='صعودی' and score>=long_threshold and gap>=min_gap_long:
            out.append(((future-entry)/entry)*100-friction)
        elif direction=='نزولی' and score<=short_threshold and gap<=min_gap_short and mom<=max_momentum_short and not(rsi<22 and mom>-0.5):
            out.append(((entry-future)/entry)*100-friction)
    return out


# ============================================================
# LIVE ENGINE
# ============================================================


def _register_live_price(symbol: str, price: float, source: str = "REST") -> None:
    if price <= 0: return
    symbol = _normalize_symbol(symbol)
    with LIVE_LOCK:
        previous = LIVE_PRICES.get(symbol, {})
        # Prefer real 24h change from ticker; fall back to session delta only if needed
        ch_24 = None
        try:
            t24 = (get_24h_tickers_cached([symbol]) or {}).get(symbol) or {}
            if t24 and math.isfinite(safe_float(t24.get("change_pct_24h"), float("nan"))):
                ch_24 = float(t24["change_pct_24h"])
        except Exception:
            _swallow()
        if ch_24 is None:
            ch_24 = ((price / previous["price"] - 1) * 100) if previous.get("price") else float(previous.get("change_pct") or 0.0)
        LIVE_PRICES[symbol] = {
            "price": float(price), "source": source, "ts": time.time(), "age_ms": 0,
            "change_pct": float(ch_24),
            "change_pct_24h": float(ch_24),
        }


def _live_status() -> dict[str, Any]:
    now = time.time()
    with LIVE_LOCK:
        rows = dict(LIVE_PRICES)
    if not rows:
        return {"status": "NO_DATA", "count": 0, "median_age_ms": None, "sources": [], "websocket": bool(_websocket_client)}
    ages = []; sources = set()
    for item in rows.values():
        age = max(0, now - float(item.get("ts", now))) * 1000; item["age_ms"] = round(age, 1); ages.append(age); sources.add(item.get("source", "REST"))
    median_age = statistics.median(ages) if ages else None
    return {"status": "LIVE" if median_age is not None and median_age < 10000 else "DELAYED", "count": len(rows), "median_age_ms": round(median_age, 1) if median_age is not None else None, "sources": sorted(sources), "websocket": bool(_websocket_client)}


def _rest_live_price_worker() -> None:
    _tick = 0
    while not LIVE_STOP.is_set():
        try:
            coins = USER_SETTINGS.get("active_coins") or DEFAULT_COINS
            if _tick % 8 == 0:  # ~ every 16s with 2s poll
                try:
                    get_24h_tickers_cached(list(coins))
                except Exception:
                    _swallow()
            for symbol, price in _fetch_live_prices(coins).items():
                _register_live_price(symbol, price, "REST")
        except Exception as exc:
            LOGGER.warning("Live REST worker failed: %s", exc)
        _tick += 1
        LIVE_STOP.wait(LIVE_PRICE_POLL_SECONDS)


def _websocket_worker() -> None:
    if _websocket_client is None: return
    while not LIVE_STOP.is_set():
        try:
            symbols = [_normalize_symbol_for_binance(s).lower() for s in (USER_SETTINGS.get("active_coins") or DEFAULT_COINS)]
            streams = "/".join(f"{s}@miniTicker" for s in symbols)
            ws = _websocket_client.create_connection(f"wss://data-stream.binance.vision/stream?streams={streams}", timeout=10)
            ws.settimeout(10)
            while not LIVE_STOP.is_set():
                raw = ws.recv()
                if not raw: break
                data = json.loads(raw).get("data", {})
                symbol = str(data.get("s", "")); price = safe_float(data.get("c"), 0)
                if symbol and price > 0: _register_live_price(symbol, price, "WebSocket")
            try: ws.close()
            except Exception: _swallow()
        except Exception as exc:
            LOGGER.info("WebSocket unavailable; REST fallback active: %s", exc); LIVE_STOP.wait(5)


def start_live_engine() -> None:
    if any(t.is_alive() for t in LIVE_THREADS): return
    LIVE_STOP.clear()
    rest = threading.Thread(target=_rest_live_price_worker, name="titan-live-rest", daemon=True)
    rest.start(); LIVE_THREADS.append(rest)
    if _websocket_client is not None:
        ws = threading.Thread(target=_websocket_worker, name="titan-live-ws", daemon=True)
        ws.start(); LIVE_THREADS.append(ws)
        # Depth only for a few liquid symbols — lower battery/CPU on mobile
        depth_t = threading.Thread(target=_depth_websocket_worker, name="titan-depth-ws", daemon=True)
        depth_t.start(); LIVE_THREADS.append(depth_t)

# ============================================================
# MARKET CACHE / COMMAND CENTER
# ============================================================


def _load_market_cache() -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    data = _load_json(MARKET_CACHE_PATH, {})
    if not isinstance(data, dict): return [], "", {}
    return data.get("data", []) if isinstance(data.get("data"), list) else [], str(data.get("gemini_summary", "") or ""), data.get("macro", {}) if isinstance(data.get("macro"), dict) else {}


def _global_ai_summary(market_data: list[dict[str, Any]], macro: dict[str, Any]) -> str:
    payload = {"macro": macro, "coins": [{"symbol": x.get("symbol"), "bias": x.get("bias"), "score": x.get("score"), "alignment": x.get("alignment")} for x in market_data[:8]]}
    return _call_gemini(payload, global_summary=True) or ""


def _market_pulse_snapshot(market_data: list[dict[str, Any]], macro: dict[str, Any]) -> dict[str, Any]:
    items = market_data or []
    scores = [safe_float(x.get("score"), 50) for x in items]
    alignments = [safe_float(x.get("alignment"), 50) for x in items]
    # Prefer unified stance family so breadth matches cards / signals
    bullish = sum(1 for x in items if str(x.get("stance_family") or "") == "LONG" or x.get("bias") == "صعودی")
    bearish = sum(1 for x in items if str(x.get("stance_family") or "") == "SHORT" or x.get("bias") == "نزولی")
    # Avoid double-count if both set; recompute cleanly
    bullish = sum(1 for x in items if str(x.get("stance_family") or ("LONG" if x.get("bias")=="صعودی" else "")) == "LONG")
    bearish = sum(1 for x in items if str(x.get("stance_family") or ("SHORT" if x.get("bias")=="نزولی" else "")) == "SHORT")
    neutral = max(0, len(items) - bullish - bearish)
    published_long = sum(1 for x in items if str(x.get("stance") or "") == "LONG")
    published_short = sum(1 for x in items if str(x.get("stance") or "") == "SHORT")
    lean_long = sum(1 for x in items if str(x.get("stance") or "") == "LEAN_LONG")
    lean_short = sum(1 for x in items if str(x.get("stance") or "") == "LEAN_SHORT")
    avg_score = float(np.mean(scores)) if scores else 50.0
    avg_alignment = float(np.mean(alignments)) if alignments else 0.0
    leader = max(items, key=lambda x: safe_float(x.get("signal_quality"), 0), default=None)
    if avg_score >= 62: regime = "متمایل به صعود"
    elif avg_score <= 38: regime = "متمایل به نزول"
    else: regime = "خنثی / دوطرفه"
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"), "regime": regime,
        "avg_score": round(avg_score, 1), "avg_alignment": round(avg_alignment, 1),
        "breadth": {
            "bullish": bullish, "bearish": bearish, "neutral": neutral, "total": len(items),
            "published_long": published_long, "published_short": published_short,
            "lean_long": lean_long, "lean_short": lean_short,
            "actionable": published_long + published_short,
        },
        "leader": {"symbol": leader.get("symbol"), "score": leader.get("score"), "quality": leader.get("signal_quality"), "tag": leader.get("signal_tag")} if leader else None,
        "btc_trend": macro.get("btc_trend", "N/A"), "fear_greed": macro.get("fear_greed_val", "N/A"),
        "confluence_avg": round(float(np.mean([safe_float(x.get("edge",{}).get("confluence",{}).get("score"),50) for x in items])) if items else 0,1),
        "meta_accept": sum(1 for x in items if x.get("edge",{}).get("meta",{}).get("label") == "ACCEPT"),
        "defensive_count": sum(1 for x in items if x.get("edge",{}).get("governor",{}).get("state") in {"DEFENSIVE","REDUCED"}),
    }


_BACKGROUND_REFRESH_LOCK = threading.Lock()
_BACKGROUND_REFRESH_RUNNING = False
_SCAN_PROGRESS_LOCK = threading.Lock()
_SCAN_PROGRESS = {
    "status":"idle", "phase":"آماده", "started_at":0.0, "finished_at":0.0,
    "completed":0, "total":0, "percent":0, "message":"هنوز اسکن جدیدی اجرا نشده است",
    "last_success_at":0.0, "elapsed_sec":0.0, "fresh":False
}

def _scan_progress_update(**kwargs):
    with _SCAN_PROGRESS_LOCK:
        _SCAN_PROGRESS.update(kwargs)
        if _SCAN_PROGRESS.get("started_at"):
            _SCAN_PROGRESS["elapsed_sec"] = round(max(0.0, time.time()-float(_SCAN_PROGRESS["started_at"])),1)
        return dict(_SCAN_PROGRESS)

def _scan_progress_snapshot():
    with _SCAN_PROGRESS_LOCK:
        out=dict(_SCAN_PROGRESS)
        if out.get("started_at"):
            out["elapsed_sec"]=round(max(0.0,time.time()-float(out["started_at"])),1)
        return out

def _scan_supervisor_tick() -> None:
    """Android watchdog: recover a stalled scanner without starting duplicate scans."""
    try:
        p = _scan_progress_snapshot()
        if p.get("status") == "running":
            started = float(p.get("started_at") or 0.0)
            # A phone/network stall should not leave the UI permanently stuck.
            if started and time.time() - started > max(180.0, AUTO_SCAN_INTERVAL_SECONDS * 4):
                LOGGER.warning("Android scan watchdog: stale scan detected; releasing refresh gate")
                _scan_progress_update(status="stalled", phase="watchdog", message="اسکن قبلی متوقف شده بود؛ تلاش مجدد…", fresh=False)
                global _BACKGROUND_REFRESH_RUNNING
                with _BACKGROUND_REFRESH_LOCK:
                    _BACKGROUND_REFRESH_RUNNING = False
    except Exception as exc:
        LOGGER.debug("scan watchdog failed: %s", exc)


def _background_market_refresh(force: bool = False) -> bool:
    """Refresh market data outside the request thread so the dashboard can render immediately."""
    global _BACKGROUND_REFRESH_RUNNING
    with _BACKGROUND_REFRESH_LOCK:
        if _BACKGROUND_REFRESH_RUNNING:
            return False
        _BACKGROUND_REFRESH_RUNNING = True
    def _runner():
        global _BACKGROUND_REFRESH_RUNNING
        try:
            update_cache(force)
        except Exception as exc:
            LOGGER.exception("Background market refresh failed: %s", exc)
        finally:
            with _BACKGROUND_REFRESH_LOCK:
                _BACKGROUND_REFRESH_RUNNING = False
    threading.Thread(target=_runner, name="titan-market-refresh", daemon=True).start()
    return True

def _fast_market_snapshot() -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    """Return memory cache first, then disk cache, without network calls."""
    with CACHE_LOCK:
        cached_data = list(CACHE.get("data") or [])
        cached_summary = CACHE.get("gemini_summary") or ""
        cached_macro = CACHE.get("macro") or {}
    if cached_data:
        # Single-source price rule: even RAM-cache hits must pass through the
        # same LIVE_PRICES rebase before any API/dashboard consumer receives them.
        try:
            live_data = _v29_sync_snapshot_to_live(cached_data)
            with CACHE_LOCK:
                CACHE["data"] = live_data
            return live_data, cached_summary, cached_macro
        except Exception as exc:
            LOGGER.debug("Live RAM snapshot sync failed: %s", exc)
            return cached_data, cached_summary, cached_macro
    data, summary, macro = _load_market_cache()
    if data:
        # Preserve the timestamp written by the scan instead of replacing it
        # with application restart time. This is critical for truthful freshness.
        raw=_load_json(MARKET_CACHE_PATH,{})
        stored_ts=safe_float(raw.get("timestamp"),0.0) if isinstance(raw,dict) else 0.0
        with CACHE_LOCK:
            CACHE.update(timestamp=stored_ts or time.time(), data=data, gemini_summary=summary, macro=macro)
    # Critical consistency rule: cached analytical state may be 40s old, but the
    # displayed market price must always come from the freshest live ticker.
    if data:
        try:
            data = _v29_sync_snapshot_to_live(list(data))
            with CACHE_LOCK:
                CACHE["data"] = data
        except Exception as exc:
            LOGGER.debug("Live snapshot sync failed: %s", exc)
    return data or [], summary or "", macro or {}

