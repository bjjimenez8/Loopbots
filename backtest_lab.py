from __future__ import annotations

from pathlib import Path
from typing import Any

from run_backtest import fetch_candles, run_loop_optimizer


MAX_BACKTEST_DAYS = 1825
VALID_TIMEFRAMES = {"15m", "30m", "1h", "2h", "4h", "1d"}
VALID_SETUPS = {"auto", "fast", "balanced", "slow"}


def run_interactive_backtest(query: dict[str, list[str]], project_root: Path) -> dict[str, Any]:
    bot_type = "loop"

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

    return _run_loop_backtest(query, project_root, defaults)


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

def _defaults(query: dict[str, list[str]], bot_type: str) -> dict[str, Any]:
    days = max(30, min(MAX_BACKTEST_DAYS, int(_query_number(query, "days", 365))))
    setup = _query_text(query, "setup", "auto").lower()
    if setup not in VALID_SETUPS:
        setup = "auto"
    timeframe = _query_text(query, "timeframe", "15m")
    if timeframe not in VALID_TIMEFRAMES:
        timeframe = "15m"
    return {
        "symbol": _query_text(query, "symbol", "DOGE/USDT").upper(),
        "days": days,
        "setup": setup,
        "timeframe": timeframe,
        "investment": max(1.0, float(_query_number(query, "investment", 1000.0))),
        "max_pairs": max(1, min(30, int(_query_number(query, "max_pairs", 10)))),
        "max_days": MAX_BACKTEST_DAYS,
        "distances": _query_text(query, "distances", "0.8,1,1.2,1.5,2,2.5,3"),
        "order_counts": _query_text(query, "order_counts", "10,20,40"),
        "hold_days": _query_text(query, "hold_days", "30"),
        "take_profit_pct": _query_text(query, "take_profit_pct", "8"),
        "stop_loss_pct": _query_text(query, "stop_loss_pct", "5"),
        "min_starts": _query_text(query, "min_starts", "3"),
        "min_win_rate": _query_text(query, "min_win_rate", "45"),
    }


def _timeframes_for_setup(setup: str, bot_type: str, selected_timeframe: str) -> list[str]:
    mapping = {
        "auto": ["15m", "30m", "1h"],
        "fast": ["15m", "30m"],
        "balanced": ["30m", "1h"],
        "slow": ["1h", "2h", "4h"],
    }
    return list(mapping.get(setup, mapping["auto"]))


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


def _quality_summary(rows: list[dict[str, Any]], days: int, label: str) -> str:
    if not rows:
        return f"No {label} setup passed the requested filters over {days} days."
    top = rows[0]
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
