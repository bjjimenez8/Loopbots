from __future__ import annotations

from typing import Any

import pandas as pd


def market_type(candles: pd.DataFrame, config: dict[str, Any] | None = None) -> str:
    config = config or {}
    sideways = _sideways_score(candles, config)
    return "sideways" if sideways["sideways_ready"] else "trend"


def mode_allowed(mode: dict[str, Any], candles: pd.DataFrame, symbol: str | None = None) -> bool:
    if symbol:
        base_asset = symbol.split("/", 1)[0].upper()
        allowed_bases = {base.upper() for base in mode.get("allowed_base_assets", [])}
        excluded_bases = {base.upper() for base in mode.get("excluded_base_assets", [])}
        if allowed_bases and base_asset not in allowed_bases:
            return False
        if base_asset in excluded_bases:
            return False

    required_market_type = mode.get("market_type", "any")
    if required_market_type == "any":
        return True
    return market_type(candles, mode.get("market_type_rules", {})) == required_market_type


def _sideways_score(candles: pd.DataFrame, config: dict[str, Any]) -> dict[str, Any]:
    lookback = int(config.get("lookback", 96))
    min_candles = int(config.get("min_candles", 72))
    min_range_width_pct = float(config.get("min_range_width_pct", 2.0))
    max_range_width_pct = float(config.get("max_range_width_pct", 14.0))
    max_ema_slope_pct = float(config.get("max_ema_slope_pct", 1.8))
    min_support_touches = int(config.get("min_support_touches", 2))
    min_resistance_touches = int(config.get("min_resistance_touches", 2))
    min_range_position = float(config.get("min_range_position", 0.12))
    max_range_position = float(config.get("max_range_position", 0.88))

    if len(candles) < min_candles:
        return {"sideways_ready": False, "reason": "not enough candles"}

    window = candles.tail(lookback).copy()
    close = pd.to_numeric(window["close"], errors="coerce")
    high = pd.to_numeric(window["high"], errors="coerce")
    low = pd.to_numeric(window["low"], errors="coerce")
    if close.isna().any() or high.isna().any() or low.isna().any():
        return {"sideways_ready": False, "reason": "bad candle data"}

    last_close = float(close.iloc[-1])
    range_high = float(high.max())
    range_low = float(low.min())
    range_span = range_high - range_low
    if last_close <= 0 or range_span <= 0:
        return {"sideways_ready": False, "reason": "invalid range"}

    range_width_pct = (range_span / last_close) * 100
    ema = close.ewm(span=min(50, max(10, len(close) // 2)), adjust=False).mean()
    slope_lookback = min(24, max(3, len(ema) - 1))
    ema_slope_pct = abs((float(ema.iloc[-1]) / float(ema.iloc[-slope_lookback]) - 1) * 100)

    support_level = range_low + (range_span * 0.22)
    resistance_level = range_high - (range_span * 0.22)
    support_touches = int((low <= support_level).sum())
    resistance_touches = int((high >= resistance_level).sum())
    range_position = (last_close - range_low) / range_span

    sideways_ready = (
        min_range_width_pct <= range_width_pct <= max_range_width_pct
        and ema_slope_pct <= max_ema_slope_pct
        and support_touches >= min_support_touches
        and resistance_touches >= min_resistance_touches
        and min_range_position <= range_position <= max_range_position
    )

    return {
        "sideways_ready": sideways_ready,
        "range_width_pct": round(range_width_pct, 2),
        "ema_slope_pct": round(ema_slope_pct, 2),
        "support_touches": support_touches,
        "resistance_touches": resistance_touches,
        "range_position": round(range_position, 2),
    }
