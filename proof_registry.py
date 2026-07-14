from __future__ import annotations

import json
from pathlib import Path
from typing import Any


PROOF_MODEL = "adaptive-proof-v1"


class AdaptiveProofRegistry:
    """Loads offline proof results and fails closed on missing or invalid data."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._profiles = self._load_profiles()

    def _load_profiles(self) -> list[dict[str, Any]]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return []
        if payload.get("version") != PROOF_MODEL or not isinstance(payload.get("profiles"), list):
            return []
        return [row for row in payload["profiles"] if isinstance(row, dict)]

    def research_rows(self, bot: str) -> list[dict[str, Any]]:
        rows = []
        for profile in self._profiles:
            if str(profile.get("bot", "")).upper() != bot.upper():
                continue
            settings = profile.get("settings") or {}
            train = profile.get("train") or {}
            test = profile.get("test") or {}
            rows.append(
                {
                    "symbol": profile.get("symbol", ""),
                    "setup": _setup_text(bot, settings),
                    "trades": profile.get("historical_starts", 0),
                    "starts": profile.get("historical_starts", 0),
                    "win_rate_pct": test.get("win_rate_pct", 0.0),
                    "monthly_per_1k": float(test.get("monthly_pct", 0.0) or 0.0) * 10,
                    "monthly_pct": test.get("monthly_pct", 0.0),
                    "worst_drawdown_pct": test.get("worst_drawdown_pct", 0.0),
                    "fee_pct": profile.get("fee_pct", 0.0),
                    "train_avg_return_pct": train.get("avg_return_pct", 0.0),
                    "test_avg_return_pct": test.get("avg_return_pct", 0.0),
                    "non_overlapping": profile.get("historical_non_overlapping", False),
                    "status": profile.get("status", "Needs stronger proof"),
                    "target_model": profile.get("proof_model", ""),
                    "settings": settings,
                    "failed_checks": profile.get("failed_checks", []),
                }
            )
        return rows

    def loop_proof_for(self, live_row: dict[str, Any]) -> dict[str, Any]:
        return self._matching_profile("LOOP", live_row)

    def grid_proof_for(self, live_row: dict[str, Any]) -> dict[str, Any]:
        return self._matching_profile("GRID", live_row)

    def proven_profiles(self, bot: str, symbol: str = "") -> list[dict[str, Any]]:
        return [
            profile
            for profile in self._profiles
            if str(profile.get("bot", "")).upper() == bot.upper()
            and profile.get("status") == "Proven"
            and (not symbol or str(profile.get("symbol", "")).upper() == symbol.upper())
        ]

    def proven_symbols(self, bot: str) -> list[str]:
        return [str(profile.get("symbol", "")) for profile in self.proven_profiles(bot) if profile.get("symbol")]

    def _matching_profile(self, bot: str, live_row: dict[str, Any]) -> dict[str, Any]:
        symbol = str(live_row.get("symbol", "")).upper()
        for profile in self._profiles:
            if str(profile.get("bot", "")).upper() != bot or str(profile.get("symbol", "")).upper() != symbol:
                continue
            if profile.get("status") != "Proven":
                continue
            settings = profile.get("settings") or {}
            if _settings_match(bot, settings, live_row):
                return profile
        return {}


def _settings_match(bot: str, settings: dict[str, Any], row: dict[str, Any]) -> bool:
    if str(settings.get("timeframe", "")) != str(row.get("timeframe", "")):
        return False
    if bot == "LOOP":
        pairs = (
            ("order_distance_pct", "order_distance_pct"),
            ("order_count", "order_count"),
            ("take_profit_pct", "take_profit_pct"),
            ("stop_loss_pct", "monitored_stop_loss_pct"),
        )
    else:
        pairs = (
            ("lower_pct", "lower_pct"),
            ("upper_pct", "upper_pct"),
            ("levels", "levels"),
            ("take_profit_pct", "take_profit_pct"),
            ("stop_loss_pct", "stop_loss_pct"),
        )
    return all(_numbers_match(settings.get(proof_key), row.get(live_key)) for proof_key, live_key in pairs)


def _numbers_match(left: Any, right: Any) -> bool:
    try:
        return abs(float(left) - float(right)) <= 0.0001
    except (TypeError, ValueError):
        return False


def _setup_text(bot: str, settings: dict[str, Any]) -> str:
    if bot.upper() == "LOOP":
        return (
            f"{settings.get('timeframe', '')} {settings.get('order_distance_pct', '')}% LOOP, "
            f"TP {settings.get('take_profit_pct', '')}% / SL {settings.get('stop_loss_pct', '')}%"
        )
    return (
        f"{settings.get('timeframe', '')} -{settings.get('lower_pct', '')}% / +{settings.get('upper_pct', '')}%, "
        f"{settings.get('levels', '')} levels"
    )


def adaptive_loop_diagnostic(profile: dict[str, Any], candles: Any, live_price: float | None = None) -> dict[str, Any]:
    settings = profile.get("settings") or {}
    lookback_bars = max(int(settings.get("lookback_days", 0) or 0) * 24, 24)
    frame = candles.copy()
    close = frame["close"].astype(float)
    frame["ema50"] = close.ewm(span=50, adjust=False).mean()
    frame["ema200"] = close.ewm(span=200, adjust=False).mean()
    frame["ema50_prior"] = frame["ema50"].shift(24)
    frame["lookback_return_pct"] = close.pct_change(lookback_bars) * 100
    frame["range_high"] = frame["high"].rolling(lookback_bars).max()
    frame["range_low"] = frame["low"].rolling(lookback_bars).min()
    span = frame["range_high"] - frame["range_low"]
    frame["range_width_pct"] = span / close * 100
    frame["range_position"] = (close - frame["range_low"]) / span
    row = frame.iloc[-1]
    required = ["ema50", "ema200", "ema50_prior", "lookback_return_pct", "range_width_pct", "range_position"]
    ready = not any(_is_missing(row.get(key)) for key in required) and _adaptive_launch_ok(row, str(settings.get("regime", "")))
    price = float(live_price if live_price is not None else row["close"])
    take_profit_pct = float(settings.get("take_profit_pct", 0.0) or 0.0)
    stop_loss_pct = float(settings.get("stop_loss_pct", 0.0) or 0.0)
    return {
        "symbol": profile.get("symbol", ""),
        "mode": f"Adaptive {settings.get('name', '')}",
        "score": 80 if ready else 48,
        "price": price,
        "reason": "READY" if ready else "historical profile is waiting for its proven market regime",
        "order_distance_pct": settings.get("order_distance_pct", ""),
        "order_count": settings.get("order_count", ""),
        "entry_zone_low": price,
        "entry_zone_high": price,
        "take_profit_price": price * (1 + take_profit_pct / 100),
        "safety_exit_price": price * (1 - stop_loss_pct / 100),
        "target_tier": settings.get("name", ""),
        "strong_momentum": settings.get("regime") == "bull",
        "take_profit_mode": "total_pnl",
        "take_profit_pct": take_profit_pct,
        "monitored_stop_loss_pct": stop_loss_pct,
        "timeframe": settings.get("timeframe", ""),
    }


def _adaptive_launch_ok(row: Any, regime: str) -> bool:
    close = float(row["close"])
    bull = close > float(row["ema200"]) and float(row["ema50"]) > float(row["ema200"]) and float(row["ema50"]) > float(row["ema50_prior"])
    sideways = (
        close > float(row["ema200"])
        and -6.0 <= float(row["lookback_return_pct"]) <= 10.0
        and 5.0 <= float(row["range_width_pct"]) <= 28.0
        and 0.15 <= float(row["range_position"]) <= 0.72
    )
    if regime == "bull":
        return bull
    if regime == "sideways_bull":
        return sideways
    return bull or sideways


def _is_missing(value: Any) -> bool:
    try:
        return value != value
    except TypeError:
        return True
