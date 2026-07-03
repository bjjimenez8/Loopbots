from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Literal


OpportunityStatus = Literal["Ready Now", "Wait", "Avoid"]
StrategyType = Literal["GRID", "LOOP"]
RiskLevel = Literal["Conservative", "Balanced", "Aggressive"]
SpeedLevel = Literal["Slow", "Medium", "Fast"]

LOOP_ESTIMATED_FEE_IMPACT_PCT = 0.40
LOOP_MIN_NET_PROFIT_PCT = 0.25


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
    grid_rows: list[dict[str, Any]],
    loop_proof_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    loop_proof = {_proof_symbol(row.get("symbol", "")): row for row in loop_proof_rows}
    opportunities: list[Opportunity] = []
    opportunities.extend(_loop_opportunity(row, loop_proof) for row in loop_rows)
    opportunities.extend(_grid_opportunity(row) for row in grid_rows)
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
    status = _practical_loop_status(debug)
    reason = _practical_reason(status, debug, strategy="LOOP")
    proof_row = proof_by_symbol.get(_proof_symbol(pair), {})
    proof = ProofStats(
        win_rate_pct=_optional_float(proof_row.get("win_rate_pct")),
        average_return_pct=_optional_float(proof_row.get("monthly_per_1k")),
        worst_drawdown_pct=None,
        historical_starts=_optional_int(proof_row.get("trades")),
        label=_proof_label(proof_row.get("status"), proof_row.get("trades")),
    )
    bitsgap_fields = {
        "Pair": pair,
        "Order distance": _pct_text(row.get("order_distance_pct")),
        "Order count": _text(row.get("order_count"), "n/a"),
        "Take profit": _price_or_note(row.get("take_profit_price"), "When strategy target is reached"),
        "Safety exit / stop guidance": _price_or_note(row.get("safety_exit_price"), "Stop if safety exit triggers"),
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


def _grid_opportunity(row: dict[str, Any]) -> Opportunity:
    pair = str(row.get("symbol", ""))
    score = int(float(row.get("score", 0) or 0))
    debug = _grid_debug(row, score)
    status = _practical_grid_status(debug)
    current_price = float(row.get("current_price", 0.0) or 0.0)
    lower_pct = float(row.get("lower_pct", 0.0) or 0.0)
    upper_pct = float(row.get("upper_pct", 0.0) or 0.0)
    take_profit_pct = float(row.get("take_profit_pct", 0.0) or 0.0)
    stop_loss_pct = float(row.get("stop_loss_pct", 0.0) or 0.0)
    low_price = current_price * (1 - lower_pct / 100) if current_price > 0 else 0.0
    high_price = current_price * (1 + upper_pct / 100) if current_price > 0 else 0.0
    take_profit_price = current_price * (1 + take_profit_pct / 100) if current_price > 0 and take_profit_pct > 0 else 0.0
    stop_loss_price = current_price * (1 - stop_loss_pct / 100) if current_price > 0 and stop_loss_pct > 0 else 0.0
    entry_zone = _grid_entry_zone(low_price, high_price)
    win_rate = _optional_float(row.get("historical_win_rate_pct"))
    avg_return = _optional_float(row.get("historical_avg_return_pct"))
    worst_drawdown = _optional_float(row.get("historical_worst_drawdown_pct"))
    has_historical_proof = bool((win_rate or 0.0) > 0 or (avg_return or 0.0) != 0 or (worst_drawdown or 0.0) != 0)
    proof = ProofStats(
        win_rate_pct=win_rate,
        average_return_pct=avg_return,
        worst_drawdown_pct=worst_drawdown,
        historical_starts=_optional_int(row.get("historical_starts")),
        label="experimental" if bool(row.get("experimental")) or not has_historical_proof else "proven",
    )
    bitsgap_fields = {
        "Pair": pair,
        "Low price": _price_or_note(low_price, "Needs live price"),
        "High price": _price_or_note(high_price, "Needs live price"),
        "Grid levels": _text(row.get("levels"), "n/a"),
        "Grid step": _pct_text(row.get("grid_step_pct")),
        "Stop loss": _stop_loss_text(stop_loss_pct, stop_loss_price),
        "Take profit": _take_profit_text(take_profit_pct, take_profit_price),
        "Trailing up/down": "Trailing Up on, Trailing Down off",
        "Profit protection": "Trail only if price keeps breaking upward. If range breaks down, respect stop loss.",
    }
    return Opportunity(
        id=f"grid:{pair}:{row.get('preset_name', '')}",
        strategy="GRID",
        pair=pair,
        status=status,
        risk=_grid_risk(row),
        speed=_grid_speed(row),
        entry_zone=entry_zone,
        bitsgap_fields=bitsgap_fields,
        proof=proof,
        reason=_practical_reason(status, debug, strategy="GRID"),
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
    tp_distance_pct = _distance_pct(entry_price, take_profit_price)
    min_tp_distance_pct = LOOP_ESTIMATED_FEE_IMPACT_PCT + LOOP_MIN_NET_PROFIT_PCT
    estimated_net_profit_pct = None
    if tp_distance_pct is not None:
        estimated_net_profit_pct = tp_distance_pct - LOOP_ESTIMATED_FEE_IMPACT_PCT
        if tp_distance_pct < min_tp_distance_pct:
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


def _grid_debug(row: dict[str, Any], score: int) -> dict[str, Any]:
    blockers = _reason_parts(str(row.get("reason", "")))
    avoid = []
    cooldown_reasons = ["recently alerted / cooldown"] if bool(row.get("cooldown")) else []
    avoid.extend(item for item in blockers if item in {"data error", "no candles", "not enough candles", "invalid range"})
    range_position = _optional_float(row.get("range_position"))
    volatility = _optional_float(row.get("range_pct"))
    if range_position is not None and not -0.15 <= range_position <= 1.15:
        avoid.append(f"price far outside range ({range_position:.2f})")
    if volatility is not None and volatility > 80:
        avoid.append(f"extreme volatility ({volatility:.2f}%)")
    return {
        "ready_blockers": blockers,
        "wait_reasons": cooldown_reasons + [item for item in blockers if item not in avoid and item != "cooldown"],
        "avoid_reasons": avoid,
        "raw_score": score,
        "range_position": range_position,
        "volatility": volatility,
        "liquidity_volume": "not available in scan record",
        "trend_regime": _grid_regime(row),
        "fee_impact": "paper fee configured in GRID tracker; not exposed per setup",
        "trend_return_pct": _optional_float(row.get("trend_return_pct")),
        "directional_efficiency": _optional_float(row.get("directional_efficiency")),
    }


def _practical_loop_status(debug: dict[str, Any]) -> OpportunityStatus:
    score = float(debug.get("raw_score") or 0.0)
    if debug.get("avoid_reasons"):
        return "Avoid"
    if score >= 35:
        return "Ready Now"
    return "Wait"


def _practical_grid_status(debug: dict[str, Any]) -> OpportunityStatus:
    score = float(debug.get("raw_score") or 0.0)
    if debug.get("avoid_reasons"):
        return "Avoid"
    if score >= 30:
        return "Ready Now"
    if score < 25:
        return "Avoid"
    return "Wait"


def _practical_reason(status: OpportunityStatus, debug: dict[str, Any], strategy: str) -> str:
    if status == "Ready Now":
        if strategy == "LOOP":
            net = debug.get("estimated_net_profit_pct")
            if isinstance(net, (int, float)):
                return f"Usable now. TP has room after fees, about {net:.2f}% net if it hits cleanly. Use the safety exit."
        return "Usable now. Range is tradable with TP, stop loss, and trailing-up protection."
    if status == "Avoid":
        reasons = debug.get("avoid_reasons") or debug.get("ready_blockers") or ["weak setup"]
        if "Take profit too close to entry. Not worth using." in reasons:
            return "Take profit too close to entry. Not worth using."
        return "Avoid: " + ", ".join(str(item) for item in reasons[:3]) + "."
    reasons = debug.get("wait_reasons") or debug.get("ready_blockers") or ["needs a cleaner setup"]
    return "Wait: " + ", ".join(str(item) for item in reasons[:3]) + "."


def _grid_regime(row: dict[str, Any]) -> str:
    trend = _optional_float(row.get("trend_return_pct"))
    directional = _optional_float(row.get("directional_efficiency"))
    if trend is None:
        return "unknown"
    if directional is not None and directional <= 0.4:
        return "sideways"
    if trend > 0:
        return "uptrend"
    return "downtrend"


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


def _grid_entry_zone(low_price: float, high_price: float) -> str:
    if low_price > 0 and high_price > 0:
        return f"{_format_price(low_price)} - {_format_price(high_price)}"
    return "Needs live price"


def _stop_loss_text(stop_loss_pct: float, stop_loss_price: float) -> str:
    pct = _pct_text(stop_loss_pct)
    if stop_loss_price > 0:
        return f"On (-{pct}) at {_format_price(stop_loss_price)}"
    return f"On (-{pct})"


def _take_profit_text(take_profit_pct: float, take_profit_price: float) -> str:
    pct = _pct_text(take_profit_pct)
    if take_profit_price > 0:
        return f"On (+{pct}) at {_format_price(take_profit_price)}"
    return f"On (+{pct})"


def _loop_risk(score: int, proof: ProofStats) -> RiskLevel:
    starts = proof.historical_starts or 0
    win_rate = proof.win_rate_pct or 0.0
    if starts >= 8 and win_rate >= 70 and score >= 85:
        return "Conservative"
    if score < 55 or starts < 5:
        return "Aggressive"
    return "Balanced"


def _grid_risk(row: dict[str, Any]) -> RiskLevel:
    if bool(row.get("experimental")):
        return "Aggressive"
    win_rate = float(row.get("historical_win_rate_pct", 0.0) or 0.0)
    avg_return = float(row.get("historical_avg_return_pct", 0.0) or 0.0)
    drawdown = abs(float(row.get("historical_worst_drawdown_pct", 0.0) or 0.0))
    if win_rate <= 0 and avg_return == 0 and drawdown == 0:
        return "Aggressive"
    if win_rate >= 70 and drawdown <= 8:
        return "Conservative"
    if win_rate < 55 or drawdown > 12:
        return "Aggressive"
    return "Balanced"


def _loop_speed(mode: str) -> SpeedLevel:
    mode = mode.lower()
    if "short" in mode or "fast" in mode:
        return "Fast"
    if "long" in mode or "slow" in mode:
        return "Slow"
    return "Medium"


def _grid_speed(row: dict[str, Any]) -> SpeedLevel:
    levels = int(float(row.get("levels", 0) or 0))
    upper_pct = float(row.get("upper_pct", 0.0) or 0.0)
    if levels <= 15 or upper_pct <= 12:
        return "Fast"
    if levels >= 50 or upper_pct >= 50:
        return "Slow"
    return "Medium"


def _proof_label(status: Any, starts: Any) -> str:
    raw = str(status or "").lower()
    count = _optional_int(starts) or 0
    if count < 5 or "small" in raw or "experimental" in raw:
        return "experimental"
    if "weak" in raw or "watch" in raw:
        return "weak data"
    return "proven"


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


def _format_price(value: float) -> str:
    if value >= 100:
        return f"{value:,.2f}"
    if value >= 1:
        return f"{value:,.4f}"
    return f"{value:,.6f}"


def opportunity_snapshot(
    loop_rows: list[dict[str, Any]],
    grid_rows: list[dict[str, Any]],
    loop_proof_rows: list[dict[str, Any]],
    strategy_filter: str = "both",
    status_filter: str = "all",
    risk_filter: str = "all",
    speed_filter: str = "all",
) -> dict[str, Any]:
    opportunities = build_opportunities(loop_rows, grid_rows, loop_proof_rows)
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
