            rw = rl = 0.0
            for i, r in enumerate(rows):
                age_days = max(
                    0.0,
                    (now - safe_float(r["created_at"], now)) / 86400.0,
                )
                w = math.exp(-age_days / 21.0) * (0.985 ** i)
                if str(r["outcome"]) == "WIN":
                    rw += w
                else:
                    rl += w

            out.update({
                "samples": wins + losses,
                "wins": wins,
                "losses": losses,
                "win_rate": round(self._beta_wr(wins, losses), 2),
                "recent_wr": round(self._beta_wr(rw, rl), 2),
                "confidence": round(clamp((wins + losses) / 40.0, 0, 1), 3),
            })
        except Exception as exc:
            LOGGER.debug("CNS history failed %s/%s: %s", symbol, direction, exc)
        return out

    def _ai_reliability(self, symbol: str, direction: str) -> dict[str, Any]:
        """Provider-specific historical alignment from persisted AI votes."""
        out = {
            "samples": 0,
            "aligned": 0,
            "alignment_pct": 50.0,
            "providers": {},
        }
        try:
            with DB_LOCK, db_conn() as con:
                rows = con.execute(
                    "SELECT provider,direction,aligned,created_at "
                    "FROM ai_votes WHERE symbol=? "
                    "ORDER BY created_at DESC LIMIT 160",
                    (symbol,),
                ).fetchall()

            aligned = 0
            total = 0
            providers: dict[str, list[int]] = {}
            for r in rows:
                if str(r["direction"] or "") != direction:
                    continue
                total += 1
                aligned += int(bool(r["aligned"]))
                providers.setdefault(str(r["provider"]), []).append(
                    int(bool(r["aligned"]))
                )

            out["samples"] = total
            out["aligned"] = aligned
            out["alignment_pct"] = round(aligned / total * 100, 1) if total else 50.0
            out["providers"] = {
                k: round(sum(v) / len(v) * 100, 1)
                for k, v in providers.items()
                if v
            }
        except Exception as exc:
            LOGGER.debug("CNS AI reliability failed %s: %s", symbol, exc)
        return out

    def _tf_evidence(
        self, tf_scores: dict[str, Any]
    ) -> tuple[float, float, dict[str, Any]]:
        long_e = short_e = 0.0
        used = 0.0
        rows = {}

        for tf, w in self.TF_WEIGHTS.items():
            if tf not in tf_scores:
                continue
            s = clamp(safe_float(tf_scores.get(tf), 50), 0, 100)
            e = (s - 50.0) / 50.0
            long_e += max(0.0, e) * w
            short_e += max(0.0, -e) * w
            used += w
            rows[tf] = {
                "score": round(s, 1),
                "edge": round(e, 3),
                "weight": w,
            }

        if used <= 0:
            return 0.0, 0.0, {"coherence": 0.0, "frames": rows}

        long_e /= used
        short_e /= used

        active = [v["edge"] for v in rows.values() if abs(v["edge"]) >= 0.08]
        if len(active) <= 1:
            coherence = 0.35 if active else 0.0
        else:
            signs = [1 if x > 0 else -1 for x in active]
            dominant = max(signs.count(1), signs.count(-1))
            coherence = dominant / len(signs)

        return (
            long_e,
            short_e,
            {"coherence": round(coherence, 3), "frames": rows},
        )

    def evaluate(self, item: dict[str, Any]) -> dict[str, Any]:
        symbol = str(item.get("symbol") or "")
        tf_scores = item.get("tf_scores") or {}
        tf_long, tf_short, tf_meta = self._tf_evidence(tf_scores)

        edge = item.get("edge") or {}
        structure = edge.get("structure") or {}
        confluence = edge.get("confluence") or {}
        regime = edge.get("regime") or {}
        precision = item.get("precision") or {}
        neural = (
            item.get("neural_v9")
            or (item.get("fusion") or {}).get("neural_v9")
            or {}
        )
        forecast = item.get("candle_forecast") or {}
        pattern = item.get("patterns") or {}
        ai = edge.get("ai") or {}
        dq = item.get("data_quality") or {}
        ladder = item.get("entry_ladder") or {}

        evidence = []

        def add(
            name: str,
            edge_value: float,
            weight: float,
            quality: float = 1.0,
            note: str = "",
        ):
            evidence.append({
                "name": name,
                "edge": clamp(edge_value, -1, 1),
                "weight": max(0.0, weight),
                "quality": clamp(quality, 0, 1),
                "note": note,
            })

        # 1) Multi-timeframe spine
        add(
            "multi_tf",
            tf_long - tf_short,
            0.22,
            tf_meta["coherence"],
            f"coherence={tf_meta['coherence']:.2f}",
        )

        # 2) Market structure
        sb = str(structure.get("bias") or "")
        sc = clamp(
            safe_float(structure.get("confirmation_score"), 50),
            0, 100,
        )
        structure_edge = 0.0
        if sb == "صعودی":
            structure_edge = (sc - 50) / 50
        elif sb == "نزولی":
            structure_edge = -(sc - 50) / 50
        add("structure", structure_edge, 0.13, sc / 100.0, sb or "neutral")

        # 3) Regime
        rn = str(regime.get("regime") or "").lower()
        reg_edge = 0.0
        if "up" in rn or "bull" in rn:
            reg_edge = 0.45
        elif "down" in rn or "bear" in rn:
            reg_edge = -0.45
        add(
            "regime",
            reg_edge,
            0.08,
            0.85 if reg_edge else 0.45,
            rn or "unknown",
        )

        # 4) Confluence
        cs = clamp(safe_float(confluence.get("score"), 50), 0, 100)
        qbias = str(item.get("bias") or "")
        confluence_edge = 0.0
        if qbias == "صعودی":
            confluence_edge = (cs - 50) / 50
        elif qbias == "نزولی":
            confluence_edge = -(cs - 50) / 50
        add(
            "confluence",
            confluence_edge,
            0.12,
            cs / 100.0,
            f"score={cs:.1f}",
        )

        # 5) Entry precision
        ps = clamp(safe_float(precision.get("score"), 50), 0, 100)
        pside = 1 if qbias == "صعودی" else -1 if qbias == "نزولی" else 0
        add(
            "precision",
            pside * (ps - 50) / 50,
            0.10,
            ps / 100.0,
            str(precision.get("entry_timing") or ""),
        )

        # 6) Probabilistic forecast path
        fb = str(forecast.get("overall_bias") or "")
        fstrength = clamp(
            safe_float(forecast.get("path_strength"), 0) / 40.0,
            0, 1,
        )
        add(
            "forecast",
            fstrength if fb == "صعودی" else -fstrength if fb == "نزولی" else 0,
            0.09,
            1.0 if forecast.get("ok") else 0.0,
            f"corr={safe_float(forecast.get('analogue_corr'), 0):.2f}",
        )

        # 7) Chart pattern
        primary = pattern.get("primary") or {}
        pb = str((primary.get("guide") or {}).get("bias") or "")
        pc = clamp(
            safe_float(primary.get("confidence"), 0) / 100.0,
            0, 1,
        )
        add(
            "pattern",
            pc if pb == "صعودی" else -pc if pb == "نزولی" else 0,
            0.06,
            pc,
            pb or "neutral",
        )

        # 8) Existing neural V9
        nside = str(neural.get("side") or "WAIT")
        nconf = clamp(
            safe_float(neural.get("confidence"), 0) / 100.0,
            0, 1,
        )
        add(
            "neural_v9",
            nconf if nside == "LONG" else -nconf if nside == "SHORT" else 0,
            0.12,
            nconf,
            f"confidence={nconf * 100:.1f}",
        )

        # 9) AI ensemble
        amaj = str(
            ai.get("majority")
            or ai.get("ai_majority")
            or item.get("ai_majority")
            or "WAIT"
        )
        aagree = clamp(
            safe_float(
                ai.get(
                    "majority_agreement",
                    ai.get(
                        "agreement",
                        (item.get("fusion") or {}).get(
                            "ai_agreement", 0
                        ),
                    ),
                ),
                0,
            ) / 100.0,
            0, 1,
        )
        add(
            "ai_ensemble",
            aagree if amaj == "LONG" else -aagree if amaj == "SHORT" else 0,
            0.10,
            aagree,
            f"majority={amaj}",
        )

        # 10) Historical memory
        long_h = self._history(symbol, "صعودی")
        short_h = self._history(symbol, "نزولی")
        hist_edge = (
            (long_h["recent_wr"] - short_h["recent_wr"]) / 50.0
        ) * 0.5
        hist_quality = min(
            1.0,
            (long_h["samples"] + short_h["samples"]) / 30.0,
        )
        add(
            "historical_memory",
            hist_edge,
            0.08,
            hist_quality,
            f"L={long_h['recent_wr']:.1f}%/{long_h['samples']} "
            f"S={short_h['recent_wr']:.1f}%/{short_h['samples']}",
        )

        total_w = sum(
            x["weight"] * max(0.05, x["quality"])
            for x in evidence
        )
        net = (
            sum(
                x["edge"] * x["weight"] * max(0.05, x["quality"])
                for x in evidence
            ) / max(total_w, 1e-9)
        )
        strength = abs(net)
        side = (
            "LONG"
            if net >= CNS_PROMOTE_MIN_MARGIN
            else "SHORT"
            if net <= -CNS_PROMOTE_MIN_MARGIN
            else "WAIT"
        )

        vetoes = []
        dq_score = safe_float(dq.get("score"), 100)

        if dq.get("hard_veto"):
            vetoes.append("data_quality")
        elif dq_score < MIN_DATA_QUALITY_SCORE:
            vetoes.append("data_quality_soft")

        if precision.get("hard_blocks"):
            # Only invalid blocks are hard; others soft
            hard_p = [x for x in precision.get("hard_blocks")[:4] if x == "invalid_price_or_atr"]
            soft_p = [x for x in precision.get("hard_blocks")[:4] if x != "invalid_price_or_atr"]
            if hard_p:
                vetoes.extend([f"precision:{x}" for x in hard_p])
            if soft_p:
                vetoes.extend([f"precision_soft:{x}" for x in soft_p])

        if safe_float(item.get("effective_rr_tp1"), 0) < CNS_PROMOTE_MIN_RR:
            # Soft RR below floor — does not block promote alone when other edges strong
            if safe_float(item.get("effective_rr_tp1"), 0) < 0.85:
                vetoes.append("rr")
            else:
                vetoes.append("rr_soft")

        if (
            ladder
            and side in {"LONG", "SHORT"}
            and not ladder.get("passed", True)
        ):
            # Soft ladder unless HTF is strongly opposite
            htf_s = safe_float(ladder.get("htf_score"), 50)
            if (side == "LONG" and htf_s <= 36) or (side == "SHORT" and htf_s >= 64):
                vetoes.append("entry_ladder")
            else:
                vetoes.append("entry_ladder_soft")

        if (
            amaj in {"LONG", "SHORT"}
            and side in {"LONG", "SHORT"}
            and amaj != side
            and aagree >= CNS_HARD_CONFLICT
        ):
            vetoes.append("ai_hard_conflict")

        chosen_hist = (
            long_h if side == "LONG" else short_h
        )
        hist_penalty = (
            chosen_hist["samples"] >= CNS_MIN_HISTORY
            and chosen_hist["recent_wr"] < 42
        )
        if hist_penalty:
            vetoes.append("weak_historical_side")

        # Promotion from WAIT requires independent evidence, not one large score.
        # Soft vetoes (suffix _soft) do not block promotion.
        quality = safe_float(item.get("signal_quality"), 0)
        hard_veto_names = {
            v for v in vetoes
            if not str(v).endswith("_soft") and "soft:" not in str(v)
        }
        promote = (
            side in {"LONG", "SHORT"}
            and strength >= CNS_PROMOTE_MIN_STRENGTH
            and abs(net) >= CNS_PROMOTE_MIN_MARGIN
            and quality >= CNS_PROMOTE_MIN_QUALITY
            and dq_score >= CNS_PROMOTE_MIN_DQ
            and nconf * 100 >= CNS_PROMOTE_MIN_NEURAL
            and safe_float(item.get("effective_rr_tp1"), 0) >= CNS_PROMOTE_MIN_RR
            and tf_meta["coherence"] >= 0.50
            and not hard_veto_names
            and not hist_penalty
        )

        existing = str(item.get("decision_tag") or "WAIT")
        opposite = (
            "SHORT"
            if existing == "LONG"
            else "LONG"
            if existing == "SHORT"
            else ""
        )

        strong_contradiction = (
            existing in {"LONG", "SHORT"}
            and side == opposite
            and strength >= 0.72
            and abs(net) >= 0.28
        )

        # Evidence-strength index; this is NOT a guaranteed win probability.
        confidence = clamp(
            50
            + strength * 45
            + tf_meta["coherence"] * 10
            - len(vetoes) * 8,
            5, 95,
        )
        if chosen_hist["samples"] < CNS_MIN_HISTORY:
            confidence = min(confidence, 82)

        action = "WAIT"
        reason = "شواهد مستقل برای تصمیم جهتی کافی نیست"

        if strong_contradiction:
            reason = "شبکه شواهد مستقل خلاف تصمیم قبلی است"
        elif existing in {"LONG", "SHORT"}:
            action = existing
            reason = "تصمیم موجود توسط شبکه شواهد مستقل بررسی و تأیید شد"
        elif promote:
            action = side
            reason = "فرصت مرزی با اجماع چندلایه و Data quality کافی ارتقا یافت"

        entry_mode = "WAIT"
        if action in {"LONG", "SHORT"}:
            timing = str(precision.get("entry_timing") or "").upper()
            if (
                bool(ladder.get("passed", False))
                and timing in {"READY", "TRIGGER", "OPTIMAL", "GOOD"}
            ):
                entry_mode = "READY"
            else:
                entry_mode = "EARLY"

        return {
            "version": CNS_VERSION,
            "action": action,
            "side": side,
            "entry_mode": entry_mode,
            "net_score": round(net, 4),
            "directional_strength": round(strength, 3),
            "confidence": round(confidence, 1),
            "promoted": bool(promote and existing == "WAIT"),
            "contradiction": bool(strong_contradiction),
            "vetoes": vetoes,
            "reason": reason,
            "tf_coherence": tf_meta["coherence"],
            "tf": tf_meta["frames"],
            "historical": {"long": long_h, "short": short_h},
            "ai_reliability": (
                self._ai_reliability(symbol, amaj)
                if amaj in {"LONG", "SHORT"}
                else {"samples": 0, "aligned": 0, "alignment_pct": 50.0, "providers": {}, "mode": "neutral"}
            ),
            "evidence": evidence,
            "independent_layers": len(
                [e for e in evidence if e["quality"] >= 0.55]
            ),
        }


TITAN_CNS = TitanCognitiveNexusV10()

# Preserve the original V10 analyzer as the audited core. The wrapper is the
# final arbitration layer used by update_cache, so the existing dashboard/API
# contract remains backward compatible.
_analyze_asset_core = _analyze_asset_base


def _v12_parse_price(value: Any) -> float:
    """Parse dashboard-formatted prices without assuming a fixed decimal format."""
    try:
        if isinstance(value, (int, float)):
            return float(value)
        s = str(value or "").replace(",", "").replace(" ", "")
        return float(s)
    except Exception:
        return 0.0


def _v12_rebuild_directional_levels(item: dict[str, Any], direction: str) -> tuple[float, float, float]:
    """Rebuild directional SL/TP when the final arbitration changes a neutral side.

    The core analyzer normally has full ATR/structure context. If a final CNS
    promotion changes WAIT -> LONG/SHORT, this guard reconstructs a conservative
    directional set from the already-computed neutral risk distance instead of
    leaving a bullish signal with bullish TP but a neutral/inconsistent SL.
    """
    price = _v12_parse_price(item.get("price"))
    old_sl = _v12_parse_price(item.get("stop_loss"))
    if price <= 0:
        return 0.0, 0.0, 0.0

    # A neutral core uses approximately 1.6 ATR plus friction. Recover a safe
    # ATR estimate from that distance, with a volatility-based floor.
    neutral_dist = abs(price - old_sl)
    vol_pct = safe_float(item.get("volatility_pct"), 0.0)
    atr_est = max(
        neutral_dist / 1.6 if neutral_dist > 0 else 0.0,
        price * max(vol_pct / 100.0, 0.0025),
        price * 0.0005,
    )

    # Use a conservative local swing proxy. This is deliberately derived from
    # existing validated price/risk data; it never invents a market level.
    swing_low = price - 2.0 * atr_est
    swing_high = price + 2.0 * atr_est

    titan = item.get("titan_analysis") or {}
    adv = titan.get("adv_layers") or {}
    # These values are optional; missing values are allowed.
    vwap = safe_float(adv.get("vwap"), 0.0)
    if direction == "LONG":
        bias = "صعودی"
    elif direction == "SHORT":
        bias = "نزولی"
    else:
        return 0.0, 0.0, 0.0

    sl, tp1, tp2 = calculate_levels(
        price=price,
        atr=atr_est,
        swing_low=swing_low,
        swing_high=swing_high,
        bias=bias,
        vwap=vwap,
        ema20=0.0,
        ema50=0.0,
        structure=(item.get("edge") or {}).get("structure") or {},
        confluence=safe_float((item.get("edge") or {}).get("confluence", {}).get("score"), 50.0),
        order_blocks_fvg=None,
        fibonacci=None,
        forecast=item.get("candle_forecast") or {},
    )
    return float(sl), float(tp1), float(tp2)


def _v12_level_integrity(price: float, sl: float, tp1: float, tp2: float, direction: str) -> dict[str, Any]:
    """Hard validation of direction/levels/RR before a directional signal is exposed."""
    if not all(math.isfinite(x) for x in (price, sl, tp1, tp2)) or price <= 0:
        return {"ok": False, "reason": "invalid_numeric_levels", "rr1": 0.0, "rr2": 0.0}

    risk = abs(price - sl)
    if risk <= price * 0.0002:
        return {"ok": False, "reason": "risk_distance_too_small", "rr1": 0.0, "rr2": 0.0}

    if direction == "LONG":
        orientation = sl < price < tp1 <= tp2
    elif direction == "SHORT":
        orientation = tp2 <= tp1 < price < sl
    else:
        orientation = False

    rr1 = abs(tp1 - price) / risk
    rr2 = abs(tp2 - price) / risk
    ok = bool(orientation and rr1 >= MIN_EFFECTIVE_RR and rr2 >= rr1)
    return {
        "ok": ok,
        "reason": "ok" if ok else ("bad_orientation" if not orientation else "rr_below_threshold"),
        "rr1": round(rr1, 3),
        "rr2": round(rr2, 3),
    }


def _v12_persist_ai_votes(symbol: str, item: dict[str, Any]) -> None:
    """Persist provider votes without creating scan-by-scan duplicate spam."""
    try:
        ai_obj = item.get("ai_opinions") or {}
        titan_side = str(item.get("decision_tag") or "WAIT")
        now = time.time()

        with DB_LOCK, db_conn() as con:
            for provider, raw in ai_obj.items():
                if (
                    provider in {"providers", "ai_status", "internal", "titan"}
                    or not isinstance(raw, str)
                    or not raw.strip()
                ):
                    continue

                vote = extract_structured_ai_vote(raw).get("side", "WAIT")
                recent = con.execute(
                    "SELECT 1 FROM ai_votes "
                    "WHERE symbol=? AND provider=? AND direction=? AND titan_direction=? "
                    "AND created_at>? LIMIT 1",
                    (symbol, provider, vote, titan_side, now - 90.0),
                ).fetchone()
                if recent:
                    continue

                con.execute(
                    "INSERT INTO ai_votes("
                    "created_at,symbol,provider,direction,titan_direction,aligned,latency_ms"
                    ") VALUES(?,?,?,?,?,?,?)",
                    (
                        now,
                        symbol,
                        provider,
                        vote,
                        titan_side,
                        int(vote == titan_side),
                        None,
                    ),
                )
    except Exception as exc:
        LOGGER.debug("V12 AI vote persistence failed %s: %s", symbol, exc)


def _v12_finalize_signal(item: dict[str, Any], cns: dict[str, Any]) -> None:
    """Apply CNS action only when the final public signal remains internally coherent."""
    current = str(item.get("decision_tag") or "WAIT")
    action = str(cns.get("action") or "WAIT")
    fusion = item.setdefault("fusion", {})
    integrity = {
        "version": "V12-CNS-PRO-MAX",
        "core_decision": current,
        "cns_proposal": action,
        "applied": False,
        "level_rebuilt": False,
        "reason": "",
    }

    if action in {"LONG", "SHORT"} and action != current:
        # If CNS proposes a new direction, rebuild levels first.
        sl, tp1, tp2 = _v12_rebuild_directional_levels(item, action)
        price = _v12_parse_price(item.get("price"))
        check = _v12_level_integrity(price, sl, tp1, tp2, action)

        if check["ok"]:
            item["stop_loss"] = smart_format(sl)
            item["tp1"] = smart_format(tp1)
            item["tp2"] = smart_format(tp2)
            item["effective_rr_tp1"] = check["rr1"]
            item["effective_rr_tp2"] = check["rr2"]
            item["rr_tp1"] = check["rr1"]
            item["rr_tp2"] = check["rr2"]
            item["decision_tag"] = action
            item["bias"] = "صعودی" if action == "LONG" else "نزولی"
            item["signal_tag"] = "CNS OPPORTUNITY" if cns.get("promoted") else "CNS CONFIRMED"
            fusion["cns_override"] = True
            fusion["cns_override_reason"] = cns.get("reason")
            integrity.update(applied=True, level_rebuilt=True, reason="directional_levels_rebuilt_and_validated")
        else:
            # Never expose a direction with stale/inconsistent levels.
            integrity["reason"] = f"CNS proposal rejected by level integrity: {check['reason']}"
            fusion["cns_override"] = False
            fusion["cns_override_reason"] = integrity["reason"]

    elif action == "WAIT" and current in {"LONG", "SHORT"}:
        # A strong independent contradiction is materially different from a
        # neutral/soft disagreement. Never keep a directional signal when the
        # evidence graph itself has a high-strength opposite side.
        severe = [v for v in (cns.get("vetoes") or []) if str(v) in {"data_quality", "ai_hard_conflict"}]
        contradiction = bool(cns.get("contradiction"))
        strength = safe_float(cns.get("directional_strength"), 0)
        if contradiction or (severe and strength < 0.35):
            item["decision_tag"] = "WAIT"
            item["bias"] = "خنثی"
            item["signal_tag"] = "CNS BLOCK"
            fusion["cns_override"] = True
            fusion["cns_override_reason"] = cns.get("reason") or "strong independent contradiction"
            integrity.update(applied=True, reason="independent_evidence_blocked_core_direction")
        else:
            fusion["cns_soft_block"] = True
            fusion["cns_override_reason"] = cns.get("reason")
            integrity.update(applied=False, reason="cns_soft_block_direction_kept")

    else:
        integrity["reason"] = "core_and_cns_decision_consistent"

    # Rebuild the canonical object after the final action, not before it.
    item["decision_integrity"] = integrity
    item["canonical_decision"] = {
        "decision": item.get("decision_tag", "WAIT"),
        "bias": item.get("bias", "خنثی"),
        "confidence": cns.get("confidence", 0),
        "signal_quality": item.get("signal_quality", 0),
        "success_probability": item.get("success_probability", 50),
        "cns_version": CNS_VERSION,
        "reason": cns.get("reason", ""),
        "entry_mode": cns.get("entry_mode", "WAIT"),
        "level_integrity": integrity["reason"],
    }


def _analyze_asset_v12_impl(symbol: str, btc_trend: str) -> Optional[dict[str, Any]]:
    """V12 CNS PRO MAX public analyzer.

    The V10/V11 core remains the data/indicator/forecast engine. This final
    layer performs evidence arbitration, decision integrity, and learning-memory
    hygiene so no final LONG/SHORT can be emitted with contradictory levels.
    """
    item = _analyze_asset_core(symbol, btc_trend)
    if not item:
        return None

    try:
        cns = TITAN_CNS.evaluate(item)
        item["cognitive_nexus"] = cns
        item.setdefault("fusion", {})["cognitive_nexus"] = cns
        item["cns_action"] = cns.get("action", "WAIT")
        item["cns_confidence"] = cns.get("confidence", 0)
        item["cns_strength"] = cns.get("directional_strength", 0)
        item["entry_mode"] = cns.get("entry_mode", "WAIT")

        _v12_finalize_signal(item, cns)

        # If a promoted signal changed direction, cap displayed confidence until
        # its first live outcome exists. This avoids presenting a fresh promotion
        # as if it were historically proven.
        if cns.get("promoted") and item.get("decision_tag") in {"LONG", "SHORT"}:
            item["signal_quality"] = int(clamp(
                safe_float(item.get("signal_quality"), 0), 0, 92
            ))

        _v12_persist_ai_votes(symbol, item)

        return item

    except Exception as exc:
        LOGGER.exception("V12 CNS arbitration failed for %s: %s", symbol, exc)
        item["decision_integrity"] = {
            "version": "V12-CNS-PRO-MAX",
            "applied": False,
            "reason": "CNS failure; original core decision retained",
        }
        item["cognitive_nexus"] = {
            "version": CNS_VERSION,
            "action": item.get("decision_tag", "WAIT"),
            "confidence": 0,
            "reason": "CNS unavailable; core decision retained",
        }
        return item



# ============================================================
# TITAN V21 TRUST MAX
# Data Integrity + Regime Intelligence + Memory + AI Reliability
# + Probability Calibration + Walk-Forward Audit + Risk Governor
#
# This layer is deliberately additive: the existing V12 dashboard/theme/API
# remain intact. The engine gains a closed-loop audit and trust architecture.
# ============================================================

V21_VERSION = "TITAN-V21-TRUST-MAX-BALANCED"
V21_MIN_DATA_TRUST = 50.0
V21_MIN_SIGNAL_TRUST = 46.0
V21_MIN_HISTORY_SAMPLES = 6
V21_CALIBRATION_MIN_SAMPLES = 10
V21_WALK_FORWARD_MIN_SAMPLES = 16
V21_MAX_STALE_SEC = 60.0
V21_HISTORY_DECAY_DAYS = 45.0


def _v21_now_ts() -> float:
    return time.time()


def _v21_num(x: Any, default: float = 0.0) -> float:
    return safe_float(x, default)


def _v21_direction(x: Any) -> str:
    s = str(x or "").upper().strip()
    return s if s in {"LONG", "SHORT", "WAIT"} else "WAIT"


def _v21_nested(obj: Any, *keys: str, default: Any = None) -> Any:
    cur = obj
    for key in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return default if cur is None else cur


class TitanDataIntegrityV21:
    """Independent data-trust gate.

    It never fabricates missing market data. Missing evidence reduces trust and
    can veto a directional decision.
    """

    def evaluate(self, item: dict[str, Any]) -> dict[str, Any]:
        checks = {}
        price = _v21_num(item.get("price"))
        checks["price"] = price > 0

        dq = _v21_num(item.get("data_quality"), 0)
        if dq == 0:
            dq = _v21_num(item.get("data_quality_score"), 0)
        checks["reported_quality"] = dq >= MIN_DATA_QUALITY_SCORE

        # Missing age is unknown, not fresh. Treating it as age=0 silently
        # converted stale/missing live prices into trusted data.
        raw_age = item.get("price_age_sec")
        age = _v21_num(raw_age, -1.0)
        checks["price_fresh"] = 0 <= age <= V21_MAX_STALE_SEC

        tf_scores = item.get("tf_scores") or {}
        valid_tfs = []
        for tf in ("15m", "1h", "4h", "1d"):
            v = _v21_num(tf_scores.get(tf), -1)
            if 0 <= v <= 100:
                valid_tfs.append(tf)
        checks["multi_tf"] = len(valid_tfs) >= 3

        forecast = item.get("candle_forecast") or item.get("forecast") or {}
        checks["forecast"] = bool(forecast.get("ok", False)) and bool(
            forecast.get("candles") or forecast.get("overall_bias") or forecast.get("expected_move_pct") is not None
        )

        derivatives = item.get("derivatives") or {}
        checks["derivatives"] = bool(derivatives) or bool(item.get("oi_delta") is not None)

        passed = sum(bool(v) for v in checks.values())
        completeness = passed / max(len(checks), 1) * 100.0

        # Quality is not allowed to exceed the evidence actually present.
        trust = 0.55 * completeness + 0.45 * min(max(dq, 0), 100)
        trust = clamp(trust, 0, 100)

        hard_fail = []
        if not checks["price"]:
            hard_fail.append("invalid_price")
        if not checks["multi_tf"]:
            hard_fail.append("insufficient_timeframes")
        if age >= 0 and age > V21_MAX_STALE_SEC:
            hard_fail.append("stale_price")
        elif age < 0:
            # Closed-candle price is still real market data; absence of a live
            # tick lowers completeness/trust but is not an invented price.
            pass

        return {
            "version": V21_VERSION,
            "trust": round(trust, 1),
            "completeness": round(completeness, 1),
            "checks": checks,
            "valid_timeframes": valid_tfs,
            "hard_fail": hard_fail,
            "passed": not hard_fail and trust >= V21_MIN_DATA_TRUST,
        }


class TitanRegimeIntelligenceV21:
    """Market-regime classifier using existing item evidence.

    Regime is descriptive; it does not directly force a direction.
    """

    def evaluate(self, item: dict[str, Any]) -> dict[str, Any]:
        tf = item.get("tf_scores") or {}
        vals = [_v21_num(tf.get(k), 50) for k in ("15m", "1h", "4h", "1d")]
        spread = max(vals) - min(vals) if vals else 0
        avg = sum(vals) / max(len(vals), 1)

        edge = item.get("edge") or {}
        regime_obj = edge.get("regime") or {}
        name = str(regime_obj.get("regime") or regime_obj.get("name") or "").lower()

        if "trend" in name or "bull" in name or "bear" in name:
            regime = "TREND"
        elif "range" in name or "side" in name:
            regime = "RANGE"
        elif "break" in name:
            regime = "BREAKOUT"
        elif spread >= 28:
            regime = "TRANSITION"
        elif 43 <= avg <= 57:
            regime = "RANGE"
        else:
            regime = "TREND"

        coherence = 100.0 - clamp(spread * 2.0, 0, 75)
        if regime == "TRANSITION":
            coherence *= 0.75

        return {
            "version": V21_VERSION,
            "regime": regime,
            "tf_average": round(avg, 1),
            "tf_spread": round(spread, 1),
            "coherence": round(coherence, 1),
        }


class TitanHistoricalMemoryV21:
    """Outcome memory with recency weighting and regime/direction separation."""

    def _rows(self, symbol: str, direction: str, limit: int = 400):
        try:
            with DB_LOCK, db_conn() as con:
                return con.execute(
                    "SELECT created_at, outcome, timeframe FROM forecasts "
                    "WHERE symbol=? AND direction=? AND outcome IN ('WIN','LOSS') "
                    "ORDER BY created_at DESC LIMIT ?",
                    (symbol, direction, limit),
                ).fetchall()
        except Exception as exc:
            LOGGER.debug("V21 memory read failed %s: %s", symbol, exc)
            return []

    def evaluate(self, symbol: str, direction: str) -> dict[str, Any]:
        direction = _v21_direction(direction)
        if direction not in {"LONG", "SHORT"}:
            return {"samples": 0, "win_rate": 50.0, "recent_win_rate": 50.0, "confidence": 0, "status": "NEUTRAL"}

        rows = self._rows(symbol, direction)
        if not rows:
            return {"samples": 0, "win_rate": 50.0, "recent_win_rate": 50.0, "confidence": 0, "status": "NO_HISTORY"}

        now = _v21_now_ts()
        weighted_wins = weighted_total = 0.0
        wins = 0
        for row in rows:
            try:
                ts = float(row[0])
            except Exception:
                ts = now
            age_days = max(0.0, (now - ts) / 86400.0) if ts > 1_000_000_000 else 0.0
            w = math.exp(-age_days / V21_HISTORY_DECAY_DAYS)
            outcome = str(row[1] or "").upper()
            weighted_total += w
            if outcome == "WIN":
                weighted_wins += w
                wins += 1

        n = len(rows)
        # Beta(2,2) shrinkage protects against tiny samples.
        posterior = (weighted_wins + 2.0) / (weighted_total + 4.0) if weighted_total else 0.5
        recent = rows[:min(20, n)]
        recent_wins = sum(1 for r in recent if str(r[1]).upper() == "WIN")
        recent_wr = recent_wins / len(recent) if recent else 0.5

        confidence = clamp(
            25.0 + min(55.0, n * 3.0) + abs(posterior - 0.5) * 80.0,
            0, 95
        )
        return {
            "samples": n,
            "wins": wins,
            "losses": n - wins,
            "win_rate": round(posterior * 100, 1),
            "recent_win_rate": round(recent_wr * 100, 1),
            "confidence": round(confidence, 1),
            "status": "ESTABLISHED" if n >= V21_MIN_HISTORY_SAMPLES else "LIMITED",
        }


class TitanAICredibilityV21:
    """Provider-specific historical reliability.

    AI output is evidence, never an unconditional override.
    """

    def evaluate(self, symbol: str, titan_direction: str) -> dict[str, Any]:
        titan_direction = _v21_direction(titan_direction)
        if titan_direction not in {"LONG", "SHORT"}:
            return {"samples": 0, "agreement": 50.0, "providers": {}, "status": "NEUTRAL"}

        try:
            with DB_LOCK, db_conn() as con:
                rows = con.execute(
                    "SELECT provider, direction, aligned FROM ai_votes "
                    "WHERE symbol=? ORDER BY created_at DESC LIMIT 500",
                    (symbol,),
                ).fetchall()
        except Exception as exc:
            LOGGER.debug("V21 AI history failed %s: %s", symbol, exc)
            rows = []

        providers = {}
        total = aligned = 0
        for provider, direction, _stored_titan_direction, is_aligned, _created_at in rows:
            p = str(provider or "").strip().lower()
            if not p:
                continue
            bucket = providers.setdefault(p, {"samples": 0, "aligned": 0})
            bucket["samples"] += 1
            bucket["aligned"] += int(bool(is_aligned))
            total += 1
            aligned += int(bool(is_aligned))

        return {
            "samples": total,
            "agreement": round(aligned / total * 100, 1) if total else 50.0,
            "providers": providers,
            "status": "ESTABLISHED" if total >= V21_MIN_HISTORY_SAMPLES else "LIMITED",
        }