from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import ccxt
import pandas as pd

from run_backtest import apply_preset, discover_backtest_pairs, fetch_candles, load_public_config


@dataclass(frozen=True)
class GridPreset:
    name: str
    lower_pct: float
    upper_pct: float
    levels: int


GRID_PRESETS = [
    GridPreset("short", lower_pct=6.5, upper_pct=7.5, levels=35),
    GridPreset("mid", lower_pct=14.0, upper_pct=17.0, levels=50),
    GridPreset("long", lower_pct=25.0, upper_pct=35.0, levels=80),
]

HOT_GRID_PRESETS = [
    GridPreset("hot_tight", lower_pct=3.0, upper_pct=4.0, levels=10),
    GridPreset("hot_balanced", lower_pct=6.5, upper_pct=7.5, levels=20),
    GridPreset("hot_wide", lower_pct=10.0, upper_pct=12.0, levels=30),
]


STABLE_BASES = {
    "USDT",
    "USDC",
    "DAI",
    "PYUSD",
    "FDUSD",
    "TUSD",
    "USDE",
    "USDG",
    "EUR",
    "USD",
    "GBP",
    "AUD",
    "CAD",
    "JPY",
}


LEVERAGED_SUFFIXES = ("UP", "DOWN", "3L", "3S", "5L", "5S", "BULL", "BEAR")


OPTIMIZER_LOWER_PCTS = [3.0, 5.0, 6.5, 8.0, 10.0, 12.0, 14.0, 18.0, 25.0]
OPTIMIZER_UPPER_PCTS = [4.0, 6.0, 7.5, 10.0, 12.0, 15.0, 17.0, 22.0, 35.0]
OPTIMIZER_LEVELS = [10, 15, 20, 30, 35, 50, 65, 80]


def backtest_grid(
    candles: pd.DataFrame,
    preset: GridPreset,
    investment: float,
    fee_pct: float,
    stop_loss_pct: float | None = None,
    take_profit_pct: float | None = None,
) -> dict[str, Any]:
    if candles.empty:
        raise ValueError("No candles to backtest")

    start_price = float(candles["close"].iloc[0])
    low_price = start_price * (1 - preset.lower_pct / 100)
    high_price = start_price * (1 + preset.upper_pct / 100)
    return simulate_grid(
        candles=candles,
        low_price=low_price,
        high_price=high_price,
        levels=preset.levels,
        investment=investment,
        fee_pct=fee_pct,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
    )


def backtest_grid_fixed_range(
    candles: pd.DataFrame,
    low_price: float,
    high_price: float,
    levels: int,
    investment: float,
    fee_pct: float,
    stop_loss_pct: float | None = None,
    take_profit_pct: float | None = None,
    start_when_inside: bool = False,
) -> dict[str, Any]:
    if candles.empty:
        raise ValueError("No candles to backtest")

    test_candles = candles
    if start_when_inside:
        inside = candles[(candles["low"] <= high_price) & (candles["high"] >= low_price)]
        if inside.empty:
            return _empty_result(candles, low_price, high_price, levels, investment, reason="never_inside_range")
        start_index = int(inside.index[0])
        test_candles = candles.iloc[start_index:].reset_index(drop=True)

    return simulate_grid(
        candles=test_candles,
        low_price=low_price,
        high_price=high_price,
        levels=levels,
        investment=investment,
        fee_pct=fee_pct,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
    )


def rolling_backtest_grid(
    candles: pd.DataFrame,
    preset: GridPreset,
    investment: float,
    fee_pct: float,
    hold_days: float,
    step_days: float,
    launch_filter: str,
    filter_lookback_days: float,
    stop_loss_pct: float | None = None,
    take_profit_pct: float | None = None,
) -> dict[str, Any]:
    if candles.empty:
        raise ValueError("No candles to backtest")
    if hold_days <= 0 or step_days <= 0:
        raise ValueError("Rolling hold-days and step-days must be positive")

    timestamps = pd.to_numeric(candles["timestamp"], errors="coerce")
    if timestamps.isna().any():
        raise ValueError("Rolling backtest requires numeric millisecond timestamps")

    hold_ms = int(hold_days * 86400 * 1000)
    step_ms = int(step_days * 86400 * 1000)
    first_ts = int(timestamps.iloc[0])
    last_ts = int(timestamps.iloc[-1])
    latest_start = last_ts - hold_ms
    if latest_start <= first_ts:
        return _empty_rolling_result(preset, hold_days, reason="not_enough_history")

    starts: list[dict[str, Any]] = []
    checked_starts = 0
    start_time = first_ts
    start_idx = 0
    min_rows = 10
    while start_time <= latest_start and start_idx < len(candles):
        while start_idx < len(candles) and int(timestamps.iloc[start_idx]) < start_time:
            start_idx += 1
        if start_idx >= len(candles):
            break

        end_time = start_time + hold_ms
        end_idx = start_idx
        while end_idx < len(candles) and int(timestamps.iloc[end_idx]) <= end_time:
            end_idx += 1

        checked_starts += 1
        if not _passes_launch_filter(candles, timestamps, start_idx, launch_filter, filter_lookback_days):
            start_time += step_ms
            continue

        window = candles.iloc[start_idx:end_idx].reset_index(drop=True)
        if len(window) >= min_rows:
            result = backtest_grid(
                window,
                preset=preset,
                investment=investment,
                fee_pct=fee_pct,
                stop_loss_pct=stop_loss_pct,
                take_profit_pct=take_profit_pct,
            )
            if result["status"] == "ok":
                starts.append(result)

        start_time += step_ms

    if not starts:
        return _empty_rolling_result(preset, hold_days, reason="no_valid_starts")

    sample_days = max((last_ts - first_ts) / (86400 * 1000), 1)
    returns = pd.Series([float(start["total_pnl_pct"]) for start in starts])
    drawdowns = pd.Series([float(start["max_drawdown_pct"]) for start in starts])
    range_breaks = pd.Series([float(start["range_break_pct"]) for start in starts])
    cycles = pd.Series([float(start["cycles"]) for start in starts])
    exit_reasons = pd.Series([str(start.get("exit_reason", "end_of_window")) for start in starts])
    win_rate = float((returns > 0).mean() * 100)
    avg_return = float(returns.mean())
    avg_monthly = avg_return * (30 / hold_days)
    return {
        "status": "ok",
        "hold_days": hold_days,
        "checked_starts": checked_starts,
        "starts": len(starts),
        "launches_per_month": round((len(starts) / sample_days) * 30, 2),
        "win_rate_pct": round(win_rate, 2),
        "avg_return_pct": round(avg_return, 2),
        "median_return_pct": round(float(returns.median()), 2),
        "p10_return_pct": round(float(returns.quantile(0.10)), 2),
        "worst_return_pct": round(float(returns.min()), 2),
        "best_return_pct": round(float(returns.max()), 2),
        "avg_monthly_pct": round(avg_monthly, 2),
        "avg_max_drawdown_pct": round(float(drawdowns.mean()), 2),
        "worst_max_drawdown_pct": round(float(drawdowns.min()), 2),
        "avg_range_break_pct": round(float(range_breaks.mean()), 2),
        "avg_cycles": round(float(cycles.mean()), 1),
        "take_profit_rate_pct": round(float((exit_reasons == "take_profit").mean() * 100), 2),
        "stop_loss_rate_pct": round(float((exit_reasons == "stop_loss").mean() * 100), 2),
    }


def optimize_grid(
    candles: pd.DataFrame,
    presets: list[GridPreset],
    investment: float,
    fee_pct: float,
    hold_days: float,
    step_days: float,
    launch_filter: str,
    filter_lookback_days: float,
    stop_loss_pct: float | None,
    take_profit_pct: float | None,
    min_starts: int,
    min_win_rate_pct: float,
    min_avg_return_pct: float,
    min_p10_return_pct: float,
    min_avg_monthly_pct: float,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for preset in presets:
        result = rolling_backtest_grid(
            candles,
            preset=preset,
            investment=investment,
            fee_pct=fee_pct,
            hold_days=hold_days,
            step_days=step_days,
            launch_filter=launch_filter,
            filter_lookback_days=filter_lookback_days,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
        )
        if result["status"] != "ok":
            continue
        if int(result["starts"]) < min_starts:
            continue
        if float(result["win_rate_pct"]) < min_win_rate_pct:
            continue
        if float(result["avg_return_pct"]) < min_avg_return_pct:
            continue
        if float(result["p10_return_pct"]) < min_p10_return_pct:
            continue
        if float(result["avg_monthly_pct"]) < min_avg_monthly_pct:
            continue

        row = {
            "preset": preset.name,
            "lower_pct": preset.lower_pct,
            "upper_pct": preset.upper_pct,
            "levels": preset.levels,
            **result,
        }
        row["optimizer_score"] = _optimizer_score(row)
        rows.append(row)

    rows.sort(key=lambda row: -float(row["optimizer_score"]))
    return rows


def _optimizer_score(row: dict[str, Any]) -> float:
    win_rate = float(row.get("win_rate_pct", 0.0))
    avg_return = float(row.get("avg_return_pct", 0.0))
    avg_monthly = float(row.get("avg_monthly_pct", 0.0))
    p10_return = float(row.get("p10_return_pct", 0.0))
    worst_return = float(row.get("worst_return_pct", 0.0))
    avg_drawdown = abs(float(row.get("avg_max_drawdown_pct", 0.0)))
    worst_drawdown = abs(float(row.get("worst_max_drawdown_pct", 0.0)))
    stop_rate = float(row.get("stop_loss_rate_pct", 0.0))
    starts = min(float(row.get("starts", 0.0)), 60.0)
    return round(
        (win_rate - 50) * 0.35
        + avg_return * 3.0
        + avg_monthly * 1.2
        + p10_return * 1.4
        + worst_return * 0.25
        + starts * 0.05
        - avg_drawdown * 0.55
        - worst_drawdown * 0.15
        - stop_rate * 0.06,
        2,
    )


def _passes_launch_filter(
    candles: pd.DataFrame,
    timestamps: pd.Series,
    start_idx: int,
    launch_filter: str,
    lookback_days: float,
) -> bool:
    if launch_filter == "none":
        return True

    start_time = int(timestamps.iloc[start_idx])
    lookback_ms = int(lookback_days * 86400 * 1000)
    lookback = candles[(timestamps >= start_time - lookback_ms) & (timestamps < start_time)]
    if len(lookback) < 20:
        return False

    first_close = float(lookback["close"].iloc[0])
    current_close = float(lookback["close"].iloc[-1])
    high_price = float(lookback["high"].max())
    low_price = float(lookback["low"].min())
    if first_close <= 0 or current_close <= 0 or low_price <= 0 or high_price <= low_price:
        return False

    trend_return_pct = ((current_close / first_close) - 1) * 100
    range_pct = ((high_price / low_price) - 1) * 100
    directional_efficiency = abs(trend_return_pct) / max(range_pct, 0.01)
    range_position = (current_close - low_price) / (high_price - low_price)

    if launch_filter == "sideways":
        return (
            -8 <= trend_return_pct <= 12
            and 5 <= range_pct <= 35
            and directional_efficiency <= 0.55
            and 0.15 <= range_position <= 0.90
        )

    if launch_filter == "strict-sideways":
        return (
            -5 <= trend_return_pct <= 8
            and 5 <= range_pct <= 25
            and directional_efficiency <= 0.40
            and 0.20 <= range_position <= 0.80
        )

    raise ValueError(f"Unknown launch filter: {launch_filter}")


def discover_grid_pairs(args: argparse.Namespace) -> list[str]:
    exchange_class = getattr(ccxt, args.exchange)
    exchange = exchange_class({"enableRateLimit": True})
    exchange.load_markets()
    tickers = exchange.fetch_tickers()
    quote_asset = args.quote_asset.upper()

    candidates: list[dict[str, Any]] = []
    for symbol, market in exchange.markets.items():
        if market.get("quote") != quote_asset:
            continue
        if not market.get("spot", False) or not market.get("active", True):
            continue

        base = str(market.get("base", "")).upper()
        if not base or base in STABLE_BASES or base.endswith(LEVERAGED_SUFFIXES):
            continue
        if not _is_plain_symbol(base):
            continue

        ticker = tickers.get(symbol) or {}
        last_price = _number(ticker.get("last"), ticker.get("close"), 0.0)
        high_price = _number(ticker.get("high"), last_price, 0.0)
        low_price = _number(ticker.get("low"), last_price, 0.0)
        quote_volume = _quote_volume(ticker, last_price)
        volatility_pct = ((high_price - low_price) / low_price) * 100 if low_price > 0 else 0.0
        change_pct = abs(_number(ticker.get("percentage"), 0.0))

        if last_price < args.grid_min_last_price:
            continue
        if quote_volume < args.grid_min_quote_volume:
            continue
        if volatility_pct < args.grid_min_volatility_pct or volatility_pct > args.grid_max_volatility_pct:
            continue
        if change_pct > args.grid_max_24h_change_pct:
            continue

        candidates.append(
            {
                "symbol": symbol,
                "quote_volume": quote_volume,
                "volatility_pct": volatility_pct,
                "change_pct": change_pct,
                "last_price": last_price,
            }
        )

    candidates.sort(
        key=lambda item: (
            -float(item["volatility_pct"]),
            -float(item["quote_volume"]),
            item["symbol"],
        )
    )
    return [item["symbol"] for item in candidates[: args.max_pairs]]


def _number(*values: Any) -> float:
    for value in values:
        try:
            if value is None:
                continue
            number = float(value)
            if pd.notna(number):
                return number
        except (TypeError, ValueError):
            continue
    return 0.0


def _is_plain_symbol(text: str) -> bool:
    return text.isascii() and text.replace("_", "").replace("-", "").isalnum()


def _quote_volume(ticker: dict[str, Any], last_price: float) -> float:
    quote_volume = _number(ticker.get("quoteVolume"))
    if quote_volume > 0:
        return quote_volume
    base_volume = _number(ticker.get("baseVolume"))
    return base_volume * last_price if base_volume > 0 and last_price > 0 else 0.0


def simulate_grid(
    candles: pd.DataFrame,
    low_price: float,
    high_price: float,
    levels: int,
    investment: float,
    fee_pct: float,
    stop_loss_pct: float | None = None,
    take_profit_pct: float | None = None,
) -> dict[str, Any]:
    if low_price <= 0 or high_price <= low_price:
        raise ValueError("Grid high price must be above low price")
    if levels < 5 or levels > 100:
        raise ValueError("Grid levels must be between 5 and 100")

    start_price = float(candles["close"].iloc[0])
    if start_price <= low_price or start_price >= high_price:
        return _empty_result(candles, low_price, high_price, levels, investment, reason="start_outside_range")

    grid_ratio = (high_price / low_price) ** (1 / levels)
    grid_prices = [low_price * (grid_ratio**index) for index in range(levels + 1)]
    grid_step_pct = (grid_ratio - 1) * 100
    current_index = max(
        0,
        min(
            levels - 1,
            max(index for index, grid_price in enumerate(grid_prices[:-1]) if grid_price <= start_price),
        ),
    )

    buy_levels = list(range(0, current_index + 1))
    sell_levels = list(range(current_index + 1, levels + 1))
    denominator = len(buy_levels) + sum(start_price / grid_prices[level] for level in sell_levels)
    order_quote = investment / denominator if denominator > 0 else 0.0

    active_buys: dict[int, float] = {level: order_quote for level in buy_levels}
    active_sells: dict[int, float] = {level: order_quote / grid_prices[level] for level in sell_levels}
    free_quote = 0.0
    bot_profit = 0.0
    cycles = 0
    buys = 0
    sells = 0
    range_breaks = 0
    max_drawdown_pct = 0.0
    peak_value = investment
    exit_reason = "end_of_window"
    exit_price = float(candles["close"].iloc[-1])
    exit_value = investment
    stopped = False

    last_price = start_price
    for _, candle in candles.iloc[1:].iterrows():
        if float(candle["low"]) < low_price or float(candle["high"]) > high_price:
            range_breaks += 1
        for price in _candle_path(candle):
            price = float(price)
            if price <= 0:
                continue

            if price < last_price:
                for level in sorted(list(active_buys), reverse=True):
                    buy_price = grid_prices[level]
                    if price <= buy_price <= last_price:
                        reserved_quote = active_buys.pop(level)
                        fee = reserved_quote * (fee_pct / 100)
                        base_amount = (reserved_quote - fee) / buy_price
                        active_sells[level + 1] = active_sells.get(level + 1, 0.0) + base_amount
                        buys += 1

            if price > last_price:
                for level in sorted(list(active_sells)):
                    sell_price = grid_prices[level]
                    if last_price <= sell_price <= price:
                        base_amount = active_sells.pop(level)
                        gross_quote = base_amount * sell_price
                        fee = gross_quote * (fee_pct / 100)
                        net_quote = gross_quote - fee
                        replacement_quote = min(order_quote, net_quote)
                        free_quote += max(net_quote - replacement_quote, 0.0)
                        bot_profit += max(net_quote - order_quote, 0.0)
                        active_buys[level - 1] = active_buys.get(level - 1, 0.0) + replacement_quote
                        sells += 1
                        cycles += 1

            last_price = price
            value = _grid_value(free_quote, active_buys, active_sells, price)
            peak_value = max(peak_value, value)
            if peak_value > 0:
                max_drawdown_pct = min(max_drawdown_pct, ((value / peak_value) - 1) * 100)
            pnl_pct = ((value / investment) - 1) * 100 if investment else 0.0
            if take_profit_pct is not None and pnl_pct >= take_profit_pct:
                exit_reason = "take_profit"
                exit_price = price
                exit_value = value
                stopped = True
                break
            if stop_loss_pct is not None and pnl_pct <= -abs(stop_loss_pct):
                exit_reason = "stop_loss"
                exit_price = price
                exit_value = value
                stopped = True
                break

        if stopped:
            break

    final_price = exit_price if stopped else float(candles["close"].iloc[-1])
    final_value = exit_value if stopped else _grid_value(free_quote, active_buys, active_sells, final_price)
    total_pnl = final_value - investment
    bot_profit_pct = (bot_profit / investment) * 100 if investment else 0.0
    total_pnl_pct = (total_pnl / investment) * 100 if investment else 0.0
    days = _days_between(candles)
    return {
        "status": "ok",
        "start_price": round(start_price, 8),
        "final_price": round(final_price, 8),
        "low_price": round(low_price, 8),
        "high_price": round(high_price, 8),
        "grid_step_pct": round(grid_step_pct, 4),
        "levels": levels,
        "order_quote": round(order_quote, 2),
        "cycles": cycles,
        "buys": buys,
        "sells": sells,
        "bot_profit": round(bot_profit, 2),
        "bot_profit_pct": round(bot_profit_pct, 2),
        "total_pnl": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl_pct, 2),
        "avg_daily_pct": round(total_pnl_pct / days, 4) if days > 0 else 0.0,
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "range_breaks": range_breaks,
        "range_break_pct": round((range_breaks / max(len(candles) - 1, 1)) * 100, 2),
        "ending_value": round(final_value, 2),
        "exit_reason": exit_reason,
        "days": round(days, 2),
    }


def _grid_value(
    free_quote: float,
    active_buys: dict[int, float],
    active_sells: dict[int, float],
    price: float,
) -> float:
    return free_quote + sum(active_buys.values()) + sum(active_sells.values()) * price


def _empty_result(
    candles: pd.DataFrame,
    low_price: float,
    high_price: float,
    levels: int,
    investment: float,
    reason: str,
) -> dict[str, Any]:
    start_price = float(candles["close"].iloc[0]) if not candles.empty else 0.0
    final_price = float(candles["close"].iloc[-1]) if not candles.empty else 0.0
    return {
        "status": reason,
        "start_price": round(start_price, 8),
        "final_price": round(final_price, 8),
        "low_price": round(low_price, 8),
        "high_price": round(high_price, 8),
        "grid_step_pct": 0.0,
        "levels": levels,
        "order_quote": 0.0,
        "cycles": 0,
        "buys": 0,
        "sells": 0,
        "bot_profit": 0.0,
        "bot_profit_pct": 0.0,
        "total_pnl": 0.0,
        "total_pnl_pct": 0.0,
        "avg_daily_pct": 0.0,
        "max_drawdown_pct": 0.0,
        "range_breaks": 0,
        "range_break_pct": 0.0,
        "ending_value": round(investment, 2),
        "days": _days_between(candles),
    }


def _empty_rolling_result(preset: GridPreset, hold_days: float, reason: str) -> dict[str, Any]:
    return {
        "status": reason,
        "hold_days": hold_days,
        "checked_starts": 0,
        "starts": 0,
        "launches_per_month": 0.0,
        "win_rate_pct": 0.0,
        "avg_return_pct": 0.0,
        "median_return_pct": 0.0,
        "p10_return_pct": 0.0,
        "worst_return_pct": 0.0,
        "best_return_pct": 0.0,
        "avg_monthly_pct": 0.0,
        "avg_max_drawdown_pct": 0.0,
        "worst_max_drawdown_pct": 0.0,
        "avg_range_break_pct": 0.0,
        "avg_cycles": 0.0,
        "take_profit_rate_pct": 0.0,
        "stop_loss_rate_pct": 0.0,
    }


def _candle_path(candle: Any) -> list[float]:
    open_price = float(candle["open"])
    high_price = float(candle["high"])
    low_price = float(candle["low"])
    close_price = float(candle["close"])
    if close_price >= open_price:
        return [open_price, low_price, high_price, close_price]
    return [open_price, high_price, low_price, close_price]


def _days_between(candles: pd.DataFrame) -> float:
    if candles.empty or "timestamp" not in candles:
        return 0.0
    start = pd.to_datetime(candles["timestamp"].iloc[0], unit="ms", utc=True, errors="coerce")
    end = pd.to_datetime(candles["timestamp"].iloc[-1], unit="ms", utc=True, errors="coerce")
    if pd.isna(start) or pd.isna(end):
        return max(len(candles) / 96, 1)
    return max((end - start).total_seconds() / 86400, 1)


def run_grid_research(args: argparse.Namespace) -> list[dict[str, Any]]:
    config = apply_preset(load_public_config(), "dual")
    config["exchange"]["timeframe"] = args.timeframe
    if args.symbols:
        pairs = [symbol.strip().upper() for symbol in args.symbols.split(",") if symbol.strip()]
    elif args.grid_smart_scan:
        pairs = discover_grid_pairs(args)
    elif args.all_usdt_pairs:
        pairs = discover_backtest_pairs(args.exchange, config, max_pairs=args.max_pairs)
    else:
        pairs = config["pairs"]

    presets = _selected_grid_presets(args)
    rows: list[dict[str, Any]] = []
    for symbol in pairs:
        try:
            candles = fetch_candles(
                None,
                symbol,
                args.timeframe,
                args.days,
                cache_dir=Path(args.cache_dir),
                exchange_id=args.history_exchange,
            )
        except Exception as exc:
            rows.append({"symbol": symbol, "preset": "", "status": f"history_error:{exc.__class__.__name__}"})
            continue

        if candles.empty:
            rows.append({"symbol": symbol, "preset": "", "status": "history_error:empty_candles"})
            continue

        if args.optimize_grid:
            try:
                optimized_rows = optimize_grid(
                    candles,
                    presets=_optimization_presets(args),
                    investment=args.investment,
                    fee_pct=args.fee_pct,
                    hold_days=args.hold_days,
                    step_days=args.step_days,
                    launch_filter=args.launch_filter,
                    filter_lookback_days=args.filter_lookback_days,
                    stop_loss_pct=args.stop_loss_pct if args.stop_loss_pct > 0 else None,
                    take_profit_pct=args.take_profit_pct if args.take_profit_pct > 0 else None,
                    min_starts=args.min_rolling_starts,
                    min_win_rate_pct=args.min_win_rate_pct,
                    min_avg_return_pct=args.min_avg_return_pct,
                    min_p10_return_pct=args.min_p10_return_pct,
                    min_avg_monthly_pct=args.min_avg_monthly_pct,
                )
            except Exception as exc:
                rows.append({"symbol": symbol, "preset": "optimizer", "status": f"optimizer_error:{exc.__class__.__name__}"})
                continue
            rows.extend({"symbol": symbol, **row} for row in optimized_rows[: args.top_setups_per_symbol])
            continue

        if args.low_price > 0 and args.high_price > 0 and args.levels > 0:
            try:
                result = backtest_grid_fixed_range(
                    candles,
                    low_price=args.low_price,
                    high_price=args.high_price,
                    levels=args.levels,
                    investment=args.investment,
                    fee_pct=args.fee_pct,
                    stop_loss_pct=args.stop_loss_pct if args.stop_loss_pct > 0 else None,
                    take_profit_pct=args.take_profit_pct if args.take_profit_pct > 0 else None,
                    start_when_inside=args.start_when_inside,
                )
            except Exception as exc:
                rows.append({"symbol": symbol, "preset": "custom_range", "status": f"backtest_error:{exc.__class__.__name__}"})
                continue
            rows.append(_with_grid_score({"symbol": symbol, "preset": "custom_range", **result}))
            continue

        for preset in presets:
            try:
                if args.rolling:
                    result = rolling_backtest_grid(
                        candles,
                        preset=preset,
                        investment=args.investment,
                        fee_pct=args.fee_pct,
                        hold_days=args.hold_days,
                        step_days=args.step_days,
                        launch_filter=args.launch_filter,
                        filter_lookback_days=args.filter_lookback_days,
                        stop_loss_pct=args.stop_loss_pct if args.stop_loss_pct > 0 else None,
                        take_profit_pct=args.take_profit_pct if args.take_profit_pct > 0 else None,
                    )
                else:
                    result = backtest_grid(
                        candles,
                        preset,
                        investment=args.investment,
                        fee_pct=args.fee_pct,
                        stop_loss_pct=args.stop_loss_pct if args.stop_loss_pct > 0 else None,
                        take_profit_pct=args.take_profit_pct if args.take_profit_pct > 0 else None,
                    )
            except Exception as exc:
                rows.append({"symbol": symbol, "preset": preset.name, "status": f"backtest_error:{exc.__class__.__name__}"})
                continue
            rows.append(_with_grid_score({"symbol": symbol, "preset": preset.name, **result}))

    if args.max_result_drawdown_pct > 0 or args.max_result_range_break_pct < 100:
        rows = [
            row
            for row in rows
            if row.get("status") != "ok"
            or (
                abs(float(row.get("max_drawdown_pct", 0.0))) <= args.max_result_drawdown_pct
                and float(row.get("range_break_pct", 0.0)) <= args.max_result_range_break_pct
            )
        ]
    if args.optimize_grid:
        sort_key = lambda row: (
            row.get("status") != "ok",
            -float(row.get("optimizer_score", 0.0)),
            -float(row.get("win_rate_pct", 0.0)),
            -float(row.get("avg_monthly_pct", 0.0)),
        )
    elif args.rolling:
        sort_key = lambda row: (
            row.get("status") != "ok",
            -float(row.get("avg_monthly_pct", 0.0)),
            -float(row.get("p10_return_pct", 0.0)),
        )
    else:
        sort_key = lambda row: (
            row.get("status") != "ok",
            -float(row.get("grid_score", 0.0)),
            -float(row.get("total_pnl_pct", 0.0)),
            float(row.get("max_drawdown_pct", 0.0)),
        )
    rows.sort(key=sort_key)
    return rows


def _optimization_presets(args: argparse.Namespace) -> list[GridPreset]:
    lower_pcts = _parse_float_list(args.optimizer_lower_pcts, OPTIMIZER_LOWER_PCTS)
    upper_pcts = _parse_float_list(args.optimizer_upper_pcts, OPTIMIZER_UPPER_PCTS)
    levels_list = [int(value) for value in _parse_float_list(args.optimizer_levels, [float(level) for level in OPTIMIZER_LEVELS])]

    presets: list[GridPreset] = []
    seen: set[tuple[float, float, int]] = set()
    for lower_pct in lower_pcts:
        for upper_pct in upper_pcts:
            if upper_pct < lower_pct * 0.65:
                continue
            for levels in levels_list:
                if levels < 5 or levels > 100:
                    continue
                approx_step_pct = ((((1 + upper_pct / 100) / (1 - lower_pct / 100)) ** (1 / levels)) - 1) * 100
                if approx_step_pct < args.optimizer_min_grid_step_pct:
                    continue
                key = (round(lower_pct, 4), round(upper_pct, 4), levels)
                if key in seen:
                    continue
                seen.add(key)
                presets.append(GridPreset(f"opt_{lower_pct:g}_{upper_pct:g}_{levels}", lower_pct, upper_pct, levels))
    return presets


def _parse_float_list(raw_value: str, default: list[float]) -> list[float]:
    if not raw_value.strip():
        return default
    values: list[float] = []
    for item in raw_value.split(","):
        item = item.strip()
        if item:
            values.append(float(item))
    return values or default


def _with_grid_score(row: dict[str, Any]) -> dict[str, Any]:
    if row.get("status") != "ok":
        return row
    total_pnl_pct = float(row.get("total_pnl_pct", 0.0))
    bot_profit_pct = float(row.get("bot_profit_pct", 0.0))
    max_drawdown_pct = abs(float(row.get("max_drawdown_pct", 0.0)))
    range_break_pct = float(row.get("range_break_pct", 0.0))
    cycles = float(row.get("cycles", 0.0))
    row["grid_score"] = round(
        total_pnl_pct
        + min(bot_profit_pct, 40) * 0.35
        + min(cycles / 100, 10)
        - max_drawdown_pct * 0.9
        - range_break_pct * 0.12,
        2,
    )
    return row


def _selected_grid_presets(args: argparse.Namespace) -> list[GridPreset]:
    if args.custom_lower_pct > 0 and args.custom_upper_pct > 0 and args.custom_levels > 0:
        return [
            GridPreset(
                args.custom_name,
                lower_pct=args.custom_lower_pct,
                upper_pct=args.custom_upper_pct,
                levels=args.custom_levels,
            )
        ]
    if args.preset_set == "hot":
        return HOT_GRID_PRESETS
    if args.preset_set == "all":
        return HOT_GRID_PRESETS + GRID_PRESETS
    return GRID_PRESETS


def main() -> None:
    parser = argparse.ArgumentParser(description="Research Bitsgap-style GRID bot settings.")
    parser.add_argument("--exchange", default="kraken", help="Live exchange universe for pair discovery.")
    parser.add_argument("--history-exchange", default="okx", help="Exchange id used for historical candles.")
    parser.add_argument("--days", type=int, default=120, help="Number of days to backtest.")
    parser.add_argument("--timeframe", default="1h", help="Candle timeframe, such as 15m, 30m, or 1h.")
    parser.add_argument("--investment", type=float, default=1000.0, help="Investment amount per simulated grid bot.")
    parser.add_argument("--fee-pct", type=float, default=0.25, help="Fee estimate per filled order in percent.")
    parser.add_argument("--cache-dir", default="data/backtests", help="Folder for cached candle data.")
    parser.add_argument("--quote-asset", default="USDT", help="Quote asset for GRID smart scan.")
    parser.add_argument("--symbols", default="", help="Comma-separated symbols to test, such as BTC/USDT,ETH/USDT.")
    parser.add_argument("--all-usdt-pairs", action="store_true", help="Discover liquid spot USDT pairs.")
    parser.add_argument("--grid-smart-scan", action="store_true", help="Discover hot but liquid GRID research candidates.")
    parser.add_argument("--max-pairs", type=int, default=15, help="Maximum discovered pairs to test.")
    parser.add_argument("--grid-min-quote-volume", type=float, default=100_000.0, help="Minimum 24h quote volume for GRID smart scan.")
    parser.add_argument("--grid-min-last-price", type=float, default=0.000001, help="Minimum last price for GRID smart scan.")
    parser.add_argument("--grid-min-volatility-pct", type=float, default=4.0, help="Minimum 24h volatility for GRID smart scan.")
    parser.add_argument("--grid-max-volatility-pct", type=float, default=80.0, help="Maximum 24h volatility for GRID smart scan.")
    parser.add_argument("--grid-max-24h-change-pct", type=float, default=35.0, help="Avoid coins already moving too directionally in 24h.")
    parser.add_argument("--max-result-drawdown-pct", type=float, default=25.0, help="Filter one-shot results above this max drawdown. Use 0 to disable.")
    parser.add_argument("--max-result-range-break-pct", type=float, default=100.0, help="Filter one-shot results that spend too much time outside range.")
    parser.add_argument("--preset-set", choices=["default", "hot", "all"], default="default", help="GRID preset family to test.")
    parser.add_argument("--custom-lower-pct", type=float, default=0.0, help="Custom lower range percent below launch price.")
    parser.add_argument("--custom-upper-pct", type=float, default=0.0, help="Custom upper range percent above launch price.")
    parser.add_argument("--custom-levels", type=int, default=0, help="Custom grid levels for percent-based range.")
    parser.add_argument("--custom-name", default="custom_pct", help="Name for custom percent preset.")
    parser.add_argument("--low-price", type=float, default=0.0, help="Custom fixed low price for one-shot backtest.")
    parser.add_argument("--high-price", type=float, default=0.0, help="Custom fixed high price for one-shot backtest.")
    parser.add_argument("--levels", type=int, default=0, help="Custom fixed range grid levels.")
    parser.add_argument("--start-when-inside", action="store_true", help="For custom fixed range, start once price first enters range.")
    parser.add_argument("--optimize-grid", action="store_true", help="Optimize GRID ranges and levels with rolling historical tests.")
    parser.add_argument("--optimizer-lower-pcts", default="", help="Comma-separated lower range percentages for optimizer.")
    parser.add_argument("--optimizer-upper-pcts", default="", help="Comma-separated upper range percentages for optimizer.")
    parser.add_argument("--optimizer-levels", default="", help="Comma-separated grid levels for optimizer.")
    parser.add_argument("--optimizer-min-grid-step-pct", type=float, default=0.3, help="Minimum approximate grid step percentage.")
    parser.add_argument("--min-rolling-starts", type=int, default=12, help="Minimum valid rolling starts for optimizer results.")
    parser.add_argument("--min-win-rate-pct", type=float, default=55.0, help="Minimum optimizer win rate.")
    parser.add_argument("--min-avg-return-pct", type=float, default=0.25, help="Minimum average return per hold window.")
    parser.add_argument("--min-p10-return-pct", type=float, default=-6.0, help="Minimum 10th percentile return.")
    parser.add_argument("--min-avg-monthly-pct", type=float, default=0.5, help="Minimum estimated monthly percentage return.")
    parser.add_argument("--top-setups-per-symbol", type=int, default=3, help="Top optimizer setups to keep per symbol.")
    parser.add_argument("--rolling", action="store_true", help="Run rolling launch-window research instead of one fixed start.")
    parser.add_argument("--hold-days", type=float, default=7.0, help="Rolling bot holding window in days.")
    parser.add_argument("--step-days", type=float, default=1.0, help="Days between rolling bot launches.")
    parser.add_argument(
        "--launch-filter",
        choices=["none", "sideways", "strict-sideways"],
        default="none",
        help="Optional pre-launch market filter for rolling research.",
    )
    parser.add_argument("--filter-lookback-days", type=float, default=14.0, help="Lookback window for launch filters.")
    parser.add_argument("--stop-loss-pct", type=float, default=0.0, help="Optional total PNL stop loss percentage.")
    parser.add_argument("--take-profit-pct", type=float, default=0.0, help="Optional total PNL take profit percentage.")
    args = parser.parse_args()

    rows = run_grid_research(args)
    if args.optimize_grid:
        print("GRID_OPTIMIZER_RESULTS")
        for row in rows:
            if row["status"] != "ok":
                print(f"{row['symbol']}|{row['preset']}|{row['status']}")
                continue
            print(
                "|".join(
                    str(row[key])
                    for key in [
                        "symbol",
                        "preset",
                        "optimizer_score",
                        "lower_pct",
                        "upper_pct",
                        "levels",
                        "hold_days",
                        "starts",
                        "launches_per_month",
                        "win_rate_pct",
                        "avg_return_pct",
                        "median_return_pct",
                        "p10_return_pct",
                        "worst_return_pct",
                        "best_return_pct",
                        "avg_monthly_pct",
                        "avg_max_drawdown_pct",
                        "worst_max_drawdown_pct",
                        "avg_range_break_pct",
                        "avg_cycles",
                        "take_profit_rate_pct",
                        "stop_loss_rate_pct",
                    ]
                )
            )
        return

    if args.rolling:
        print("GRID_ROLLING_RESULTS")
        for row in rows:
            if row["status"] != "ok":
                print(f"{row['symbol']}|{row['preset']}|{row['status']}")
                continue
            print(
                "|".join(
                    str(row[key])
                    for key in [
                        "symbol",
                        "preset",
                        "hold_days",
                        "checked_starts",
                        "starts",
                        "launches_per_month",
                        "win_rate_pct",
                        "avg_return_pct",
                        "median_return_pct",
                        "p10_return_pct",
                        "worst_return_pct",
                        "best_return_pct",
                        "avg_monthly_pct",
                        "avg_max_drawdown_pct",
                        "worst_max_drawdown_pct",
                        "avg_range_break_pct",
                        "avg_cycles",
                        "take_profit_rate_pct",
                        "stop_loss_rate_pct",
                    ]
                )
            )
        return

    print("GRID_RESULTS")
    for row in rows:
        if row["status"] != "ok":
            print(f"{row['symbol']}|{row['preset']}|{row['status']}")
            continue
        print(
            "|".join(
                str(row[key])
                for key in [
                    "symbol",
                    "preset",
                    "grid_score",
                    "days",
                    "start_price",
                    "final_price",
                    "low_price",
                    "high_price",
                    "grid_step_pct",
                    "levels",
                    "cycles",
                    "bot_profit_pct",
                    "total_pnl_pct",
                    "avg_daily_pct",
                    "max_drawdown_pct",
                    "range_breaks",
                    "range_break_pct",
                    "ending_value",
                ]
            )
        )


if __name__ == "__main__":
    main()
