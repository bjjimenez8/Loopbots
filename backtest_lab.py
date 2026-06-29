from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from typing import Any

from run_backtest import fetch_candles, run_loop_optimizer
from run_grid_backtest import run_grid_research


MAX_BACKTEST_DAYS = 1825
VALID_TIMEFRAMES = {"15m", "30m", "1h", "2h", "4h", "1d"}
VALID_SETUPS = {"auto", "fast", "balanced", "slow"}


def run_interactive_backtest(query: dict[str, list[str]], project_root: Path) -> dict[str, Any]:
    bot_type = _query_text(query, "bot", "grid").lower()
    if bot_type not in {"grid", "loop"}:
        bot_type = "grid"

    defaults = _defaults(query, bot_type)
    resolved_symbol = _resolve_symbol(str(defaults["symbol"]), str(defaults["timeframe"]), project_root, int(defaults["days"]))
    defaults["symbol_input"] = defaults["symbol"]
    defaults["symbol"] = resolved_symbol["symbol"]
    defaults["symbol_valid"] = resolved_symbol["valid"]
    preview = _coin_preview(str(defaults["symbol"]), str(defaults["timeframe"]), project_root, int(defaults["days"]))
    if resolved_symbol["message"]:
        preview["message"] = resolved_symbol["message"]
    if not _query_bool(query, "run", False):
        return {
            **defaults,
            "ran": False,
            "bot": bot_type,
            "preview": preview,
            "summary": "Choose settings, press Run Backtest, and this page will rank the best Bitsgap-ready setup.",
            "rows": [],
        }

    if not bool(defaults["symbol_valid"]) and str(defaults["symbol"]) not in {"AUTO", "ALL", ""}:
        return {
            **defaults,
            "ran": True,
            "bot": bot_type,
            "preview": preview,
            "summary": f"{defaults['symbol_input']} is not a confirmed Kraken pair. Pick a real coin/pair before backtesting.",
            "errors": [f"No Kraken market/candles found for {defaults['symbol_input']}."],
            "rows": [],
        }

    if bot_type == "loop":
        return _run_loop_backtest(query, project_root, defaults)
    return _run_grid_backtest(query, project_root, defaults)


def _run_loop_backtest(query: dict[str, list[str]], project_root: Path, defaults: dict[str, Any]) -> dict[str, Any]:
    symbol = str(defaults.get("symbol", _query_text(query, "symbol", "DOGE/USDT"))).upper()
    scan_all = symbol in {"", "AUTO", "ALL"}
    days = int(defaults["days"])
    preview_timeframe = str(defaults["timeframe"])
    timeframes = _timeframes_for_setup(str(defaults["setup"]), bot_type="loop", selected_timeframe=preview_timeframe)
    investment = float(defaults["investment"])
    distances = _float_list(_query_text(query, "distances", "0.8,1,1.2,1.5,2,2.5,3"), [0.8, 1.0, 1.2, 1.5, 2.0, 2.5, 3.0])
    counts = [
        count
        for count in _int_list(_query_text(query, "order_counts", "10,20,40"), [10, 20, 40])
        if 10 <= count <= 40 and count % 2 == 0
    ] or [10]

    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for timeframe in timeframes:
        for order_count in counts:
            try:
                _, result_rows = run_loop_optimizer(
                    exchange_id="kraken",
                    history_exchange_id="kraken",
                    days=days,
                    fee_pct=0.2,
                    preset="dual",
                    cache_dir=project_root / "data" / "backtests",
                    starting_balance=max(investment * 4, 10_000),
                    trade_size=investment,
                    validate_markets=True,
                    all_usdt_pairs=scan_all,
                    max_pairs=int(defaults["max_pairs"]),
                    loop_distances=distances,
                    timeframe=timeframe,
                    order_count=order_count,
                    symbols=None if scan_all else [symbol],
                )
            except Exception as exc:
                errors.append(f"{symbol or 'AUTO'} {timeframe} count {order_count}: {exc.__class__.__name__}")
                continue

            for row in result_rows:
                if row.get("status") != "ok":
                    continue
                distance = float(row.get("best_distance_pct", 0.0) or 0.0)
                current_price = _latest_price(str(row["symbol"]), timeframe, project_root, days=min(days, 30))
                row = {
                    **row,
                    "timeframe": timeframe,
                    "order_count": order_count,
                    "order_distance_pct": distance,
                    "current_price": current_price,
                    **_loop_range(current_price, distance, order_count),
                }
                row["chart"] = _chart_data(str(row["symbol"]), timeframe, project_root, days=min(days, 90))
                rows.append(row)

    rows.sort(
        key=lambda row: (
            -float(row.get("monthly_return_on_trade_size_pct", 0.0)),
            -float(row.get("win_rate_pct", 0.0)),
            -int(row.get("trades", 0)),
        )
    )
    if not rows and not scan_all and bool(defaults.get("symbol_valid", False)):
        rows = [_market_backtest_row(symbol, timeframes[0], project_root, days, bot="loop")]
        errors.append("No strict LOOP setup passed, so this is the market-only backtest view.")
    best_timeframe = str(rows[0].get("timeframe", preview_timeframe)) if rows else preview_timeframe
    return {
        **defaults,
        "ran": True,
        "bot": "loop",
        "tested_timeframes": timeframes,
        "timeframe": best_timeframe,
        "preview": _locked_preview(defaults, symbol, best_timeframe, project_root, days),
        "summary": _quality_summary(rows, days, "LOOP"),
        "errors": errors,
        "rows": rows[:12],
    }

def _run_grid_backtest(query: dict[str, list[str]], project_root: Path, defaults: dict[str, Any]) -> dict[str, Any]:
    symbol = str(defaults.get("symbol", _query_text(query, "symbol", "ZEC/USD"))).upper()
    scan_all = symbol in {"", "AUTO", "ALL"}
    quote_asset = _quote_from_symbol(symbol, _query_text(query, "quote_asset", "USD").upper())
    days = int(defaults["days"])
    preview_timeframe = str(defaults["timeframe"])
    timeframes = _timeframes_for_setup(str(defaults["setup"]), bot_type="grid", selected_timeframe=preview_timeframe)
    investment = float(defaults["investment"])
    hold_days = max(3.0, min(float(_query_number(query, "hold_days", 30.0)), float(days)))
    min_starts = max(3, int(_query_number(query, "min_starts", 3)))
    errors: list[str] = []
    rows: list[dict[str, Any]] = []
    args_by_timeframe: dict[str, Namespace] = {}
    for timeframe in timeframes:
        args = _grid_args(query, defaults, quote_asset, symbol, scan_all, days, timeframe, investment, hold_days, min_starts)
        args_by_timeframe[timeframe] = args
        try:
            timeframe_rows = [row for row in run_grid_research(args) if row.get("status") == "ok"]
        except Exception as exc:
            timeframe_rows = []
            errors.append(f"{symbol or 'AUTO'} {timeframe}: {exc.__class__.__name__}")
        if not timeframe_rows and not scan_all:
            relaxed_args = Namespace(**vars(args))
            relaxed_args.launch_filter = "none"
            relaxed_args.min_rolling_starts = 1
            relaxed_args.min_win_rate_pct = 0.0
            relaxed_args.min_avg_return_pct = -100.0
            relaxed_args.min_p10_return_pct = -100.0
            relaxed_args.min_avg_monthly_pct = -100.0
            try:
                timeframe_rows = [row for row in run_grid_research(relaxed_args) if row.get("status") == "ok"]
                if timeframe_rows:
                    errors.append(f"{timeframe}: no strict setup passed, showing best relaxed result.")
            except Exception as exc:
                errors.append(f"relaxed {symbol} {timeframe}: {exc.__class__.__name__}")
        for row in timeframe_rows:
            rows.append({**row, "timeframe": timeframe})

    enriched_rows = []
    for row in rows:
        timeframe = str(row.get("timeframe", preview_timeframe))
        args = args_by_timeframe.get(timeframe) or _grid_args(query, defaults, quote_asset, symbol, scan_all, days, timeframe, investment, hold_days, min_starts)
        current_price = _latest_price(str(row["symbol"]), timeframe, project_root, days=min(days, 30))
        lower_pct = float(row.get("lower_pct", 0.0) or 0.0)
        upper_pct = float(row.get("upper_pct", 0.0) or 0.0)
        levels = int(float(row.get("levels", 0) or 0))
        enriched_rows.append(
            {
                **row,
                "timeframe": timeframe,
                "current_price": current_price,
                "low_price": current_price * (1 - lower_pct / 100) if current_price else 0.0,
                "high_price": current_price * (1 + upper_pct / 100) if current_price else 0.0,
                "grid_step_pct": _grid_step(lower_pct, upper_pct, levels),
                "order_size_currency": str(row.get("symbol", "")).split("/")[-1],
                "take_profit_pct": args.take_profit_pct,
                "stop_loss_pct": args.stop_loss_pct,
            }
        )
        enriched_rows[-1]["chart"] = _chart_data(str(row["symbol"]), timeframe, project_root, days=min(days, 90))

    enriched_rows.sort(
        key=lambda row: (
            -float(row.get("optimizer_score", 0.0)),
            -float(row.get("avg_monthly_pct", 0.0)),
            -float(row.get("win_rate_pct", 0.0)),
        )
    )
    if not enriched_rows and not scan_all and bool(defaults.get("symbol_valid", False)):
        enriched_rows = [_market_backtest_row(symbol, timeframes[0], project_root, days, bot="grid")]
        errors.append("No strict GRID setup passed, so this is the market-only backtest view.")
    best_timeframe = str(enriched_rows[0].get("timeframe", preview_timeframe)) if enriched_rows else preview_timeframe
    return {
        **defaults,
        "ran": True,
        "bot": "grid",
        "tested_timeframes": timeframes,
        "timeframe": best_timeframe,
        "preview": _locked_preview(defaults, symbol, best_timeframe, project_root, days),
        "summary": _quality_summary(enriched_rows, days, "GRID"),
        "errors": errors,
        "rows": enriched_rows[:15],
    }


def _defaults(query: dict[str, list[str]], bot_type: str) -> dict[str, Any]:
    days = max(30, min(MAX_BACKTEST_DAYS, int(_query_number(query, "days", 365))))
    setup = _query_text(query, "setup", "auto").lower()
    if setup not in VALID_SETUPS:
        setup = "auto"
    timeframe = _query_text(query, "timeframe", "1h" if bot_type == "grid" else "15m")
    if timeframe not in VALID_TIMEFRAMES:
        timeframe = "1h" if bot_type == "grid" else "15m"
    return {
        "symbol": _query_text(query, "symbol", "ZEC/USD" if bot_type == "grid" else "DOGE/USDT").upper(),
        "days": days,
        "setup": setup,
        "timeframe": timeframe,
        "investment": max(1.0, float(_query_number(query, "investment", 1000.0))),
        "max_pairs": max(1, min(30, int(_query_number(query, "max_pairs", 10)))),
        "max_days": MAX_BACKTEST_DAYS,
        "distances": _query_text(query, "distances", "0.8,1,1.2,1.5,2,2.5,3"),
        "order_counts": _query_text(query, "order_counts", "10,20,40"),
        "grid_lowers": _query_text(query, "grid_lowers", "3,5,8,10,14,18,25,35"),
        "grid_uppers": _query_text(query, "grid_uppers", "7.5,10,17,22,35,50,65,80"),
        "grid_levels": _query_text(query, "grid_levels", "5,10,20,35,50,65,85,100"),
        "hold_days": _query_text(query, "hold_days", "30"),
        "take_profit_pct": _query_text(query, "take_profit_pct", "8"),
        "stop_loss_pct": _query_text(query, "stop_loss_pct", "5"),
        "min_starts": _query_text(query, "min_starts", "3"),
        "min_win_rate": _query_text(query, "min_win_rate", "45"),
    }


def _timeframes_for_setup(setup: str, bot_type: str, selected_timeframe: str) -> list[str]:
    if bot_type == "loop":
        mapping = {
            "auto": ["15m", "30m", "1h"],
            "fast": ["15m", "30m"],
            "balanced": ["30m", "1h"],
            "slow": ["1h", "2h", "4h"],
        }
    else:
        mapping = {
            "auto": ["30m", "1h", "2h"],
            "fast": ["15m", "30m"],
            "balanced": ["30m", "1h"],
            "slow": ["2h", "4h", "1d"],
        }
    return list(mapping.get(setup, mapping["auto"]))


def _grid_args(
    query: dict[str, list[str]],
    defaults: dict[str, Any],
    quote_asset: str,
    symbol: str,
    scan_all: bool,
    days: int,
    timeframe: str,
    investment: float,
    hold_days: float,
    min_starts: int,
) -> Namespace:
    return Namespace(
        exchange="kraken",
        history_exchange="kraken",
        days=days,
        timeframe=timeframe,
        investment=investment,
        fee_pct=0.1,
        cache_dir=str(Path(__file__).resolve().parent / "data" / "backtests"),
        quote_asset=quote_asset,
        symbols="" if scan_all else symbol,
        all_usdt_pairs=False,
        grid_smart_scan=scan_all,
        max_pairs=int(defaults["max_pairs"]),
        grid_min_quote_volume=40_000.0,
        grid_min_last_price=0.000001,
        grid_min_volatility_pct=2.0,
        grid_max_volatility_pct=100.0,
        grid_max_24h_change_pct=60.0,
        max_result_drawdown_pct=0.0,
        max_result_range_break_pct=100.0,
        preset_set="all",
        custom_lower_pct=0.0,
        custom_upper_pct=0.0,
        custom_levels=0,
        custom_name="custom_pct",
        low_price=0.0,
        high_price=0.0,
        levels=0,
        start_when_inside=False,
        optimize_grid=True,
        optimizer_lower_pcts=_query_text(query, "grid_lowers", "3,5,8,10,14,18,25,35"),
        optimizer_upper_pcts=_query_text(query, "grid_uppers", "7.5,10,17,22,35,50,65,80"),
        optimizer_levels=_query_text(query, "grid_levels", "5,10,20,35,50,65,85,100"),
        optimizer_min_grid_step_pct=max(0.3, float(_query_number(query, "min_grid_step", 0.3))),
        min_rolling_starts=min_starts,
        min_win_rate_pct=float(_query_number(query, "min_win_rate", 45.0)),
        min_avg_return_pct=float(_query_number(query, "min_avg_return", 0.0)),
        min_p10_return_pct=float(_query_number(query, "min_p10_return", -12.0)),
        min_avg_monthly_pct=float(_query_number(query, "min_monthly", 0.0)),
        top_setups_per_symbol=3,
        rolling=False,
        hold_days=hold_days,
        step_days=max(1.0, min(7.0, hold_days / 4)),
        launch_filter=_query_text(query, "launch_filter", "sideways"),
        filter_lookback_days=14.0,
        stop_loss_pct=float(_query_number(query, "stop_loss_pct", 5.0)),
        take_profit_pct=float(_query_number(query, "take_profit_pct", 8.0)),
    )


def _latest_price(symbol: str, timeframe: str, project_root: Path, days: int = 30) -> float:
    try:
        candles = fetch_candles(
            None,
            symbol,
            timeframe,
            days,
            cache_dir=project_root / "data" / "backtests",
            exchange_id="kraken",
        )
    except Exception:
        return 0.0
    if candles.empty:
        return 0.0
    return float(candles["close"].iloc[-1])


def _resolve_symbol(raw_symbol: str, timeframe: str, project_root: Path, days: int) -> dict[str, Any]:
    raw_symbol = raw_symbol.upper().strip()
    if raw_symbol in {"", "AUTO", "ALL"}:
        return {"symbol": raw_symbol or "AUTO", "valid": True, "message": "AUTO scan selected. Run Backtest to search Kraken pairs."}
    candidates = [raw_symbol]
    if "/" not in raw_symbol:
        candidates = [f"{raw_symbol}/USD", f"{raw_symbol}/USDC", f"{raw_symbol}/USDT"]
    for candidate in candidates:
        if _chart_data(candidate, timeframe, project_root, days=min(max(days, 30), 120)):
            message = "Coin locked to Kraken pair. Ready to optimize."
            if candidate != raw_symbol:
                message = f"Coin locked to {candidate}. Ready to optimize."
            return {"symbol": candidate, "valid": True, "message": message}
    return {"symbol": raw_symbol, "valid": False, "message": "No Kraken candles found. This is not locked to a tradable pair."}


def _market_backtest_row(symbol: str, timeframe: str, project_root: Path, days: int, bot: str) -> dict[str, Any]:
    chart = _chart_data(symbol, timeframe, project_root, days=min(max(days, 30), 365))
    prices = [float(point.get("close", 0.0) or 0.0) for point in chart if float(point.get("close", 0.0) or 0.0) > 0]
    if not prices:
        return {"symbol": symbol, "timeframe": timeframe, "status": "missing_history", "chart": []}
    low_price = min(prices)
    high_price = max(prices)
    net_return_pct = ((prices[-1] / prices[0]) - 1) * 100 if prices[0] else 0.0
    row: dict[str, Any] = {
        "symbol": symbol,
        "timeframe": timeframe,
        "status": "market_only",
        "chart": chart,
        "current_price": prices[-1],
        "low_price": low_price,
        "high_price": high_price,
        "win_rate_pct": 0.0,
        "net_return_pct": round(net_return_pct, 2),
        "monthly_return_on_trade_size_pct": round(net_return_pct / max(days / 30, 1), 2),
        "avg_monthly_pct": round(net_return_pct / max(days / 30, 1), 2),
        "worst_max_drawdown_pct": _chart_drawdown_pct(prices),
        "trades": 0,
        "starts": 0,
    }
    if bot == "grid":
        row.update({
            "levels": 10,
            "grid_step_pct": _grid_step(5.0, 10.0, 10),
            "order_size_currency": symbol.split("/")[-1] if "/" in symbol else "USD",
            "take_profit_pct": 8.0,
            "stop_loss_pct": 5.0,
            "optimizer_score": 0.0,
        })
    else:
        row.update({"order_count": 10, "order_distance_pct": 1.0, "avg_hold_hours": 0.0})
    return row


def _chart_drawdown_pct(prices: list[float]) -> float:
    peak = 0.0
    worst = 0.0
    for price in prices:
        peak = max(peak, price)
        if peak > 0:
            worst = min(worst, ((price / peak) - 1) * 100)
    return round(worst, 2)


def _coin_preview(symbol: str, timeframe: str, project_root: Path, days: int) -> dict[str, Any]:
    symbol = symbol.upper().strip()
    if symbol in {"", "AUTO", "ALL"}:
        return {
            "symbol": symbol or "AUTO",
            "exists": False,
            "message": "AUTO scan selected. Run Backtest to search Kraken pairs.",
            "chart": [],
            "points": 0,
            "latest_price": 0.0,
        }
    chart = _chart_data(symbol, timeframe, project_root, days=min(max(days, 30), 365))
    latest_price = float(chart[-1]["close"]) if chart else 0.0
    return {
        "symbol": symbol,
        "exists": bool(chart),
        "message": "Coin found on Kraken. Ready to optimize." if chart else "No Kraken candles found for this pair/timeframe.",
        "chart": chart,
        "points": len(chart),
        "latest_price": latest_price,
    }


def _locked_preview(defaults: dict[str, Any], symbol: str, timeframe: str, project_root: Path, days: int) -> dict[str, Any]:
    preview = _coin_preview(symbol, timeframe, project_root, days)
    symbol_input = str(defaults.get("symbol_input", symbol)).upper()
    if preview["exists"] and symbol_input != symbol:
        preview["message"] = f"Coin locked to {symbol}. Ready to optimize."
    return preview


def _chart_data(symbol: str, timeframe: str, project_root: Path, days: int = 90) -> list[dict[str, float]]:
    try:
        candles = fetch_candles(
            None,
            symbol,
            timeframe,
            days,
            cache_dir=project_root / "data" / "backtests",
            exchange_id="kraken",
        )
    except Exception:
        return []
    if candles.empty:
        return []
    sample = candles.tail(220)
    return [
        {
            "close": float(row.close),
            "high": float(row.high),
            "low": float(row.low),
        }
        for row in sample.itertuples()
    ]


def _loop_range(price: float, order_distance_pct: float, order_count: int) -> dict[str, float]:
    if price <= 0 or order_distance_pct <= 0 or order_count <= 0:
        return {"low_price": 0.0, "high_price": 0.0}
    half_range_pct = order_distance_pct * (order_count / 2)
    return {
        "low_price": price * (1 - half_range_pct / 100),
        "high_price": price * (1 + half_range_pct / 100),
    }


def _grid_step(lower_pct: float, upper_pct: float, levels: int) -> float:
    if levels <= 0:
        return 0.0
    low_ratio = 1 - (lower_pct / 100)
    high_ratio = 1 + (upper_pct / 100)
    if low_ratio <= 0 or high_ratio <= low_ratio:
        return 0.0
    return ((high_ratio / low_ratio) ** (1 / levels) - 1) * 100


def _quality_summary(rows: list[dict[str, Any]], days: int, label: str) -> str:
    if not rows:
        return f"No {label} setup passed the requested filters over {days} days."
    top = rows[0]
    if label == "GRID":
        return (
            f"Best {label}: {top.get('symbol')} at {top.get('win_rate_pct', 0)}% WR, "
            f"{top.get('starts', 0)} starts, {top.get('avg_monthly_pct', 0)}% est monthly."
        )
    if int(top.get("trades", 0) or 0) <= 0:
        return f"No {label} setup produced closed trades over {days} days."
    return (
        f"Best {label}: {top.get('symbol')} at {top.get('win_rate_pct', 0)}% WR, "
        f"{top.get('trades', 0)} trades, {top.get('monthly_return_on_trade_size_pct', 0)}% est monthly."
    )


def _query_text(query: dict[str, list[str]], key: str, default: str) -> str:
    values = query.get(key)
    if not values:
        return default
    value = str(values[0]).strip()
    return value if value else default


def _query_number(query: dict[str, list[str]], key: str, default: float) -> float:
    try:
        return float(_query_text(query, key, str(default)))
    except ValueError:
        return default


def _query_bool(query: dict[str, list[str]], key: str, default: bool) -> bool:
    if key not in query:
        return default
    return _query_text(query, key, "0").lower() in {"1", "true", "yes", "on"}


def _float_list(raw_value: str, default: list[float]) -> list[float]:
    values: list[float] = []
    for item in raw_value.split(","):
        try:
            values.append(float(item.strip()))
        except ValueError:
            continue
    return values or default


def _int_list(raw_value: str, default: list[int]) -> list[int]:
    values: list[int] = []
    for item in raw_value.split(","):
        try:
            values.append(int(float(item.strip())))
        except ValueError:
            continue
    return values or default


def _quote_from_symbol(symbol: str, default: str) -> str:
    if "/" in symbol:
        return symbol.split("/")[-1].upper()
    return default
