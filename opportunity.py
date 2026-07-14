from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Literal


OpportunityStatus = Literal["Ready Now", "Wait", "Avoid"]
StrategyType = Literal["LOOP"]
RiskLevel = Literal["Conservative", "Balanced", "Aggressive"]
SpeedLevel = Literal["Slow", "Medium", "Fast"]

LOOP_ESTIMATED_FEE_IMPACT_PCT = 0.50
LOOP_MIN_GROSS_TARGET_PCT = 5.0
LOOP_MAX_GROSS_TARGET_PCT = 20.0
LOOP_MIN_NET_PROFIT_PCT = LOOP_MIN_GROSS_TARGET_PCT - LOOP_ESTIMATED_FEE_IMPACT_PCT
LOOP_PRICE_ROUNDING_TOLERANCE_PCT = 0.05
LOOP_READY_SCORE = 70
LOOP_MIN_PROOF_TRADES = 4
LOOP_MIN_PROOF_WIN_RATE_PCT = 65.0
LOOP_MIN_MONTHLY_PER_1K = 10.0
LOOP_PROOF_MODEL = "adaptive-proof-v1"


@dataclass(frozen=True)
class ProofStats:
    win_rate_pct: float | None = None
    average_return_pct: float | None = None
    worst_drawdown_pct: float | None = None
    historical_starts: int | None = None
    label: str = "experimental"


@dataclass(frozen=True)
class Opportunity:
    id: str
    strategy: StrategyType
    pair: str
    status: OpportunityStatus
    risk: RiskLevel
    speed: SpeedLevel
    entry_zone: str
    bitsgap_fields: dict[str, str]
    proof: ProofStats
    reason: str
    score: int
    using_placeholder: str = "I'm Using This Setup"


def build_opportunities(
    loop_rows: list[dict[str, Any]],
    loop_proof_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    loop_proof = {_proof_symbol(row.get("symbol", "")): row for row in loop_proof_rows}
    opportunities: list[Opportunity] = []
    opportunities.extend(_loop_opportunity(row, loop_proof) for row in loop_rows)
    opportunities.sort(
        key=lambda item: (
            _status_rank(item.status),
            -item.score,
            item.strategy,
            item.pair,
        )
    )
    return [asdict(item) for item in opportunities]


def filter_opportunities(
    opportunities: list[dict[str, Any]],
    strategy: str = "both",
    status: str = "all",
    risk: str = "all",
    speed: str = "all",
) -> list[dict[str, Any]]:
    strategy = strategy.lower()
    status = status.lower()
    risk = risk.lower()
    speed = speed.lower()
    filtered = []
    for item in opportunities:
        if strategy != "both" and item.get("strategy", "").lower() != strategy:
            continue
        if status != "all" and item.get("status", "").lower().replace(" ", "_") != status:
            continue
        if risk != "all" and item.get("risk", "").lower() != risk:
            continue
        if speed != "all" and item.get("speed", "").lower() != speed:
            continue
        filtered.append(item)
    return _dedupe_opportunities(filtered)


def _dedupe_opportunities(opportunities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, Any]] = []
    for item in opportunities:
        key = (
            str(item.get("strategy", "")).upper(),
            str(item.get("pair", "")).upper(),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _loop_opportunity(row: dict[str, Any], proof_by_symbol: dict[str, dict[str, Any]]) -> Opportunity:
    pair = str(row.get("symbol", ""))
    score = int(float(row.get("entry_score", 0) or 0))
    raw_reason = str(row.get("reason", ""))
    debug = _loop_debug(row, raw_reason, score)
    proof_row = proof_by_symbol.get(_proof_symbol(pair), {})
    proof = ProofStats(
        win_rate_pct=_optional_float(proof_row.get("win_rate_pct")),
        average_return_pct=_optional_float(proof_row.get("monthly_per_1k")),
        worst_drawdown_pct=None,
        historical_starts=_optional_int(proof_row.get("trades")),
        label=_proof_label(proof_row.get("status"), proof_row.get("trades")),
    )
    proof_matches_current = (
        str(proof_row.get("target_model", "")).lower() == LOOP_PROOF_MODEL
        and str(proof_row.get("status", "")).lower() == "proven"
        and _loop_proof_settings_match(row, proof_row.get("settings") or {})
    )
    debug["proof_matches_current_strategy"] = proof_matches_current
    status = _practical_loop_status(debug, proof, proof_matches_current=proof_matches_current)
    reason = _practical_reason(status, debug, strategy="LOOP")
    take_profit_mode = str(row.get("take_profit_mode", "price")).lower()
    take_profit_pct = _optional_float(row.get("take_profit_pct"))
    take_profit_price = _optional_float(row.get("take_profit_price"))
    monitored_stop_pct = _optional_float(row.get("monitored_stop_loss_pct"))
    take_profit_text = (
        _total_pnl_target_text(take_profit_pct, take_profit_price)
        if take_profit_mode == "total_pnl" and take_profit_pct is not None
        else _price_or_note(take_profit_price, "When strategy target is reached")
    )
    stop_loss_text = _price_or_note(row.get("safety_exit_price"), "Stop if monitored stop triggers")
    if monitored_stop_pct is not None:
        stop_loss_text = f"Monitored -{monitored_stop_pct:g}% at {stop_loss_text}"
    bitsgap_fields = {
        "Pair": pair,
        "Order distance": _pct_text(row.get("order_distance_pct")),
        "Order count": _text(row.get("order_count"), "n/a"),
        "Take profit": take_profit_text,
        "Stop loss": stop_loss_text,
        "Profit protection": "If price pushes above TP area, trail profit. If momentum weakens, exit instead of waiting.",
    }
    return Opportunity(
        id=f"loop:{pair}:{row.get('mode', '')}",
        strategy="LOOP",
        pair=pair,
        status=status,
        risk=_loop_risk(score, proof),
        speed=_loop_speed(str(row.get("mode", ""))),
        entry_zone=_entry_zone(row),
        bitsgap_fields=bitsgap_fields,
        proof=proof,
        reason=reason,
        score=score,
    )


def _simple_reason(reason: str, ready: bool, strategy: str) -> str:
    if ready:
        return f"{strategy} setup is ready based on the current scan."
    if not reason:
        return "Waiting for a cleaner setup."
    if reason == "READY":
        return f"{strategy} setup is ready based on the current scan."
    cleaned = reason.replace(";", ",")
    return f"Waiting: {cleaned}."


def _loop_debug(row: dict[str, Any], raw_reason: str, score: int) -> dict[str, Any]:
    blockers = _reason_parts(raw_reason)
    hard_avoid = [item for item in blockers if item in {"not enough data", "breakdown"}]
    has_settings = bool(row.get("order_distance_pct") and row.get("order_count"))
    if not has_settings:
        hard_avoid.append("missing Bitsgap settings")
    entry_price = _optional_float(row.get("price"))
    take_profit_price = _optional_float(row.get("take_profit_price"))
    take_profit_mode = str(row.get("take_profit_mode", "price")).lower()
    configured_target_pct = _optional_float(row.get("take_profit_pct"))
    tp_distance_pct = (
        configured_target_pct
        if take_profit_mode == "total_pnl" and configured_target_pct is not None
        else _distance_pct(entry_price, take_profit_price)
    )
    min_tp_distance_pct = LOOP_ESTIMATED_FEE_IMPACT_PCT + LOOP_MIN_NET_PROFIT_PCT
    estimated_net_profit_pct = None
    if tp_distance_pct is not None:
        estimated_net_profit_pct = (
            tp_distance_pct
            if take_profit_mode == "total_pnl"
            else tp_distance_pct - LOOP_ESTIMATED_FEE_IMPACT_PCT
        )
        if tp_distance_pct < min_tp_distance_pct - LOOP_PRICE_ROUNDING_TOLERANCE_PCT:
            hard_avoid.append("Take profit too close to entry. Not worth using.")
    return {
        "ready_blockers": blockers,
        "wait_reasons": blockers,
        "avoid_reasons": hard_avoid,
        "raw_score": _optional_float(row.get("raw_score")) or score,
        "tp_distance_pct": tp_distance_pct,
        "order_distance_pct": _optional_float(row.get("order_distance_pct")),
        "estimated_fee_impact_pct": LOOP_ESTIMATED_FEE_IMPACT_PCT,
        "minimum_net_profit_pct": LOOP_MIN_NET_PROFIT_PCT,
        "estimated_net_profit_pct": estimated_net_profit_pct,
        "target_tier": row.get("target_tier") or "Normal 5-10%",
        "strong_momentum": bool(row.get("strong_momentum")),
        "take_profit_mode": take_profit_mode,
        "range_position": _optional_float(row.get("range_position")),
        "volatility": _optional_float(row.get("volatility")),
        "liquidity_volume": _optional_float(row.get("volume_ratio")),
        "trend_regime": row.get("trend_regime") or "unknown",
        "fee_impact": row.get("fee_impact_pct") or "not available",
        "checks": {
            "trend": bool(row.get("trend_ok")),
            "ema_reclaim": bool(row.get("ema_reclaim_ok")),
            "pullback": bool(row.get("pullback_ok")),
            "bounce": bool(row.get("bounce_ok")),
            "rsi": bool(row.get("rsi_ok")),
            "volume": bool(row.get("volume_ok")),
            "breakdown": bool(row.get("breakdown_ok")),
            "range_tp": bool(row.get("range_tp_ok")),
        },
    }


def _practical_loop_status(
    debug: dict[str, Any],
    proof: ProofStats | None = None,
    proof_matches_current: bool = False,
) -> OpportunityStatus:
    score = float(debug.get("raw_score") or 0.0)
    if debug.get("avoid_reasons"):
        return "Avoid"
    net = debug.get("estimated_net_profit_pct")
    has_profit_room = (
        isinstance(net, (int, float))
        and net >= LOOP_MIN_NET_PROFIT_PCT - LOOP_PRICE_ROUNDING_TOLERANCE_PCT
    )
    gross_target = debug.get("tp_distance_pct")
    target_is_valid = (
        isinstance(gross_target, (int, float))
        and LOOP_MIN_GROSS_TARGET_PCT - LOOP_PRICE_ROUNDING_TOLERANCE_PCT
        <= gross_target
        <= LOOP_MAX_GROSS_TARGET_PCT + LOOP_PRICE_ROUNDING_TOLERANCE_PCT
    )
    proof = proof or ProofStats()
    has_proof = (
        (proof.historical_starts or 0) >= LOOP_MIN_PROOF_TRADES
        and (proof.win_rate_pct or 0.0) >= LOOP_MIN_PROOF_WIN_RATE_PCT
        and (proof.average_return_pct or 0.0) >= LOOP_MIN_MONTHLY_PER_1K
    )
    if score >= LOOP_READY_SCORE and has_profit_room and target_is_valid and has_proof and proof_matches_current:
        return "Ready Now"
    return "Wait"


def _practical_reason(status: OpportunityStatus, debug: dict[str, Any], strategy: str) -> str:
    if status == "Ready Now":
        if strategy == "LOOP":
            net = debug.get("estimated_net_profit_pct")
            if isinstance(net, (int, float)):
                return f"Usable now. TP has room after fees, about {net:.2f}% net if it hits cleanly. Use the stop loss."
        return "Usable now. Range is tradable with TP, stop loss, and trailing-up protection."
    if status == "Avoid":
        reasons = debug.get("avoid_reasons") or debug.get("ready_blockers") or ["weak setup"]
        if "Take profit too close to entry. Not worth using." in reasons:
            return "Take profit too close to entry. Not worth using."
        return "Avoid: " + ", ".join(str(item) for item in reasons[:3]) + "."
    if strategy == "LOOP" and not debug.get("proof_matches_current_strategy"):
        return "Wait: this bull-regime Total PnL strategy has not passed its current proof test."
    if strategy == "LOOP" and not (debug.get("wait_reasons") or debug.get("ready_blockers")):
        return "Wait: the current bull-regime Total PnL proof is below the required win rate."
    reasons = debug.get("wait_reasons") or debug.get("ready_blockers") or ["needs a cleaner setup"]
    return "Wait: " + ", ".join(str(item) for item in reasons[:3]) + "."


def _reason_parts(reason: str) -> list[str]:
    cleaned = str(reason or "").strip()
    if not cleaned or cleaned == "READY":
        return []
    return [part.strip() for chunk in cleaned.split(";") for part in chunk.split(",") if part.strip()]


def _distance_pct(entry_price: float | None, target_price: float | None) -> float | None:
    if entry_price is None or target_price is None or entry_price <= 0 or target_price <= 0:
        return None
    return ((target_price / entry_price) - 1) * 100


def _entry_zone(row: dict[str, Any]) -> str:
    low = _optional_float(row.get("entry_zone_low"))
    high = _optional_float(row.get("entry_zone_high"))
    price = _optional_float(row.get("price"))
    if low and high:
        return f"{_format_price(low)} - {_format_price(high)}"
    if price:
        return f"Around {_format_price(price)}"
    return "Wait for next ready signal"


def _loop_risk(score: int, proof: ProofStats) -> RiskLevel:
    starts = proof.historical_starts or 0
    win_rate = proof.win_rate_pct or 0.0
    if proof.label == "proven" and starts >= 8 and win_rate >= 70 and score >= 85:
        return "Conservative"
    if score < 55 or starts < 5:
        return "Aggressive"
    return "Balanced"


def _loop_speed(mode: str) -> SpeedLevel:
    mode = mode.lower()
    if "short" in mode or "fast" in mode:
        return "Fast"
    if "long" in mode or "slow" in mode:
        return "Slow"
    return "Medium"


def _proof_label(status: Any, starts: Any) -> str:
    raw = str(status or "").lower()
    count = _optional_int(starts) or 0
    if count < 5 or "small" in raw or "experimental" in raw:
        return "experimental"
    if "weak" in raw or "watch" in raw or "failed" in raw:
        return "weak data"
    if "legacy" in raw:
        return "legacy proof"
    return "proven"


def _loop_proof_settings_match(row: dict[str, Any], settings: dict[str, Any]) -> bool:
    pairs = (
        (row.get("timeframe"), settings.get("timeframe")),
        (row.get("order_distance_pct"), settings.get("order_distance_pct")),
        (row.get("order_count"), settings.get("order_count")),
        (row.get("take_profit_pct"), settings.get("take_profit_pct")),
        (row.get("monitored_stop_loss_pct"), settings.get("stop_loss_pct")),
    )
    for live, proven in pairs:
        if str(live) == str(proven):
            continue
        try:
            if abs(float(live) - float(proven)) <= 0.0001:
                continue
        except (TypeError, ValueError):
            pass
        return False
    return True


def _status_rank(status: str) -> int:
    return {"Ready Now": 0, "Wait": 1, "Avoid": 2}.get(status, 9)


def _proof_symbol(symbol: Any) -> str:
    return str(symbol or "").split("/")[0].upper()


def _optional_float(value: Any) -> float | None:
    try:
        if value in {"", None}:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    try:
        if value in {"", None}:
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _pct_text(value: Any) -> str:
    number = _optional_float(value)
    if number is None:
        return "n/a"
    return f"{number:.2f}".rstrip("0").rstrip(".") + "%"


def _text(value: Any, fallback: str) -> str:
    if value in {"", None}:
        return fallback
    return str(value)


def _price_or_note(value: Any, note: str) -> str:
    number = _optional_float(value)
    if number is None or number <= 0:
        return note
    return _format_price(number)


def _total_pnl_target_text(target_pct: float, reference_price: float | None) -> str:
    target = f"Total PnL +{target_pct:g}%"
    if reference_price is None or reference_price <= 0:
        return target
    return f"{target} (approx. coin price {_format_price(reference_price)})"


def _format_price(value: float) -> str:
    if value >= 100:
        return f"{value:,.2f}"
    if value >= 1:
        return f"{value:,.4f}"
    return f"{value:,.6f}"


def opportunity_snapshot(
    loop_rows: list[dict[str, Any]],
    loop_proof_rows: list[dict[str, Any]],
    strategy_filter: str = "both",
    status_filter: str = "all",
    risk_filter: str = "all",
    speed_filter: str = "all",
) -> dict[str, Any]:
    opportunities = build_opportunities(loop_rows, loop_proof_rows)
    filtered = filter_opportunities(
        opportunities,
        strategy=strategy_filter,
        status=status_filter,
        risk=risk_filter,
        speed=speed_filter,
    )
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "filters": {
            "strategy": strategy_filter,
            "status": status_filter,
            "risk": risk_filter,
            "speed": speed_filter,
        },
        "counts": {
            "total": len(opportunities),
            "filtered": len(filtered),
            "ready_now": sum(1 for item in filtered if item.get("status") == "Ready Now"),
            "wait": sum(1 for item in filtered if item.get("status") == "Wait"),
            "avoid": sum(1 for item in filtered if item.get("status") == "Avoid"),
        },
        "opportunities": filtered,
    }
