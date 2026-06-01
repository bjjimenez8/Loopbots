from __future__ import annotations

import argparse
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import ccxt
import pandas as pd

from strategy import LoopStrategy


DEFAULT_CONFIG: dict[str, Any] = {
    "exchange": {
        "id": "kraken",
        "enable_rate_limit": True,
        "sandbox": False,
        "timeframe": "15m",
    },
    "pairs": [
        "BTC/USDT",
        "ETH/USDT",
        "DOGE/USDT",
        "LINK/USDT",
        "SOL/USDT",
    ],
    "strategy": {
        "ema_fast": 9,
        "ema_slow": 21,
        "ema_trend": 50,
        "rsi_period": 14,
        "atr_period": 14,
        "min_rsi": 42,
        "max_rsi": 68,
        "pullback_lookback": 6,
        "pullback_min_pct": 0.002,
        "pullback_max_pct": 0.035,
        "bounce_confirmation_pct": 0.0015,
        "volume_sma_period": 20,
        "min_volume_ratio": 0.85,
        "take_profit_atr_multiple": 1.4,
        "safety_exit_atr_multiple": 0.9,
        "max_active_minutes": 180,
    },
    "loop_settings": {
        "preset_name": "Mid-term",
        "order_distance_pct": 1.5,
        "order_count": 10,
        "assumed_round_trip_fee_pct": 0.2,
        "suggested_profit_pct": 0.006,
        "suggested_safety_exit_pct": 0.004,
        "max_loop_count": 3,
        "quote_amount_usdt": 100,
    },
}

PRESET_OVERRIDES: dict[str, dict[str, Any]] = {
    "short": {
        "loop_settings": {
            "preset_name": "Short-term",
            "order_distance_pct": 1.0,
            "order_count": 10,
        },
    },
    "mid": {
        "loop_settings": {
            "preset_name": "Mid-term",
            "order_distance_pct": 1.5,
            "order_count": 10,
        },
    },
    "long": {
        "loop_settings": {
            "preset_name": "Long-term",
            "order_distance_pct": 3.0,
            "order_count": 10,
        },
    },
}


def _safe_symbol(symbol: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", symbol).strip("_")


def load_public_config() -> dict[str, Any]:
    config_path = Path(__file__).resolve().parent / "config.yaml"
    try:
        import yaml  # type: ignore

        with config_path.open("r", encoding="utf-8") as file:
            loaded = yaml.safe_load(file)
        return {
            "exchange": {
                "id": loaded["exchange"]["id"],
                "enable_rate_limit": loaded["exchange"]["enable_rate_limit"],
                "sandbox": loaded["exchange"]["sandbox"],
                "timeframe": loaded["exchange"]["timeframe"],
            },
            "pairs": loaded["pairs"],
            "strategy": loaded["strategy"],
            "loop_settings": loaded["loop_settings"],
        }
    except Exception:
        return DEFAULT_CONFIG


def apply_preset(config: dict[str, Any], preset: str) -> dict[str, Any]:
    preset_key = preset.lower()
    overrides = PRESET_OVERRIDES.get(preset_key, {})
    merged = {
        "exchange": dict(config["exchange"]),
        "pairs": list(config["pairs"]),
        "strategy": dict(config["strategy"]),
        "loop_settings": dict(config["loop_settings"]),
    }
    if "strategy" in overrides:
        merged["strategy"].update(overrides["strategy"])
    if "loop_settings" in overrides:
        merged["loop_settings"].update(overrides["loop_settings"])
    return merged


def fetch_candles(
    exchange: Any,
    symbol: str,
    timeframe: str,
    days: int,
    limit: int = 300,
    cache_dir: Path | None = None,
) -> pd.DataFrame:
    cache_path = None
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_name = f"{exchange.id}_{_safe_symbol(symbol)}_{timeframe}_{days}d.csv"
        cache_path = cache_dir / cache_name
        if cache_path.exists():
            cached = pd.read_csv(cache_path)
            for column in ["open", "high", "low", "close", "volume"]:
                cached[column] = pd.to_numeric(cached[column], errors="coerce")
            return cached.dropna().reset_index(drop=True)

    since_ms = int((datetime.now(UTC) - timedelta(days=days)).timestamp() * 1000)
    rows: list[list[Any]] = []
    next_since = since_ms

    while True:
        batch = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=next_since, limit=limit)
        if not batch:
            break

        rows.extend(batch)
        if len(batch) < limit:
            break

        candidate_since = batch[-1][0] + 1
        if candidate_since <= next_since:
            break

        next_since = candidate_since
        if len(rows) > 30000:
            break

    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates(subset=["timestamp"]).reset_index(drop=True)
    for column in ["open", "high", "low", "close", "volume"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df = df.dropna().reset_index(drop=True)
    if cache_path is not None:
        df.to_csv(cache_path, index=False)
    return df


def run_backtest(
    exchange_id: str,
    days: int,
    fee_pct: float,
    preset: str,
    cache_dir: Path | None = None,
    starting_balance: float = 10000.0,
    trade_size: float | None = None,
    history_exchange_id: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    config = apply_preset(load_public_config(), preset)
    config["exchange"]["id"] = exchange_id

    live_exchange_class = getattr(ccxt, exchange_id)
    live_exchange = live_exchange_class({"enableRateLimit": config["exchange"]["enable_rate_limit"]})
    live_exchange.load_markets()
    history_exchange_name = history_exchange_id or exchange_id
    history_exchange_class = getattr(ccxt, history_exchange_name)
    history_exchange = history_exchange_class({"enableRateLimit": config["exchange"]["enable_rate_limit"]})
    history_exchange.load_markets()
    strategy = LoopStrategy(config["strategy"], config["loop_settings"])
    fixed_trade_size = float(trade_size if trade_size is not None else config["loop_settings"]["quote_amount_usdt"])

    summary = {
        "exchange": exchange_id,
        "history_exchange": history_exchange_name,
        "preset": preset,
        "days": days,
        "fee_pct": fee_pct,
        "starting_balance": starting_balance,
        "trade_size": fixed_trade_size,
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "gross_return_pct": 0.0,
        "net_return_pct": 0.0,
    }
    results: list[dict[str, Any]] = []
    candles_by_symbol: dict[str, pd.DataFrame] = {}

    for symbol in config["pairs"]:
        if symbol not in live_exchange.markets:
            results.append({"symbol": symbol, "status": "missing"})
            continue
        if symbol not in history_exchange.markets:
            results.append({"symbol": symbol, "status": "missing_history"})
            continue

        candles = fetch_candles(history_exchange, symbol, config["exchange"]["timeframe"], days, cache_dir=cache_dir)
        candles_by_symbol[symbol] = candles
        active_trade: dict[str, Any] | None = None
        wins = 0
        losses = 0
        gross_return_pct = 0.0
        net_return_pct = 0.0
        hold_bars: list[int] = []

        for index in range(strategy._minimum_candles, len(candles)):
            window = candles.iloc[: index + 1].reset_index(drop=True)

            if active_trade is None:
                signal = strategy.analyze_entry(symbol, window)
                if signal.signal_type == "ENTER":
                    active_trade = {
                        "entry_price": signal.price,
                        "take_profit_price": float(signal.take_profit_price),
                        "safety_exit_price": float(signal.safety_exit_price),
                        "entry_index": index,
                    }
                continue

            price = float(window["close"].iloc[-1])
            trade_return_pct = ((price / active_trade["entry_price"]) - 1) * 100
            if price >= active_trade["take_profit_price"]:
                wins += 1
                gross_return_pct += trade_return_pct
                net_return_pct += trade_return_pct - fee_pct
                hold_bars.append(index - active_trade["entry_index"])
                active_trade = None
            elif price <= active_trade["safety_exit_price"]:
                losses += 1
                gross_return_pct += trade_return_pct
                net_return_pct += trade_return_pct - fee_pct
                hold_bars.append(index - active_trade["entry_index"])
                active_trade = None

        trades = wins + losses
        results.append(
            {
                "symbol": symbol,
                "status": "ok",
                "candles": len(candles),
                "trades": trades,
                "wins": wins,
                "losses": losses,
                "win_rate_pct": round((wins / trades * 100), 2) if trades else 0.0,
                "gross_return_pct": round(gross_return_pct, 2),
                "net_return_pct": round(net_return_pct, 2),
                "avg_hold_hours": round((sum(hold_bars) / len(hold_bars) * 0.25), 2) if hold_bars else 0.0,
            }
        )
        summary["trades"] += trades
        summary["wins"] += wins
        summary["losses"] += losses
        summary["gross_return_pct"] += gross_return_pct
        summary["net_return_pct"] += net_return_pct

    summary["gross_return_pct"] = round(summary["gross_return_pct"], 2)
    summary["net_return_pct"] = round(summary["net_return_pct"], 2)
    summary["win_rate_pct"] = round((summary["wins"] / summary["trades"] * 100), 2) if summary["trades"] else 0.0
    balance_summary = simulate_portfolio(
        candles_by_symbol=candles_by_symbol,
        strategy=strategy,
        fee_pct=fee_pct,
        starting_balance=starting_balance,
        trade_size=fixed_trade_size,
    )
    summary.update(balance_summary)
    return summary, results


def simulate_portfolio(
    candles_by_symbol: dict[str, pd.DataFrame],
    strategy: LoopStrategy,
    fee_pct: float,
    starting_balance: float,
    trade_size: float,
) -> dict[str, Any]:
    cash_balance = float(starting_balance)
    active_trades: dict[str, dict[str, Any]] = {}
    timeline: list[tuple[int, str, int]] = []

    for symbol, candles in candles_by_symbol.items():
        for index in range(strategy._minimum_candles, len(candles)):
            timeline.append((int(candles.iloc[index]["timestamp"]), symbol, index))

    timeline.sort()
    max_concurrent = 0

    for _, symbol, index in timeline:
        candles = candles_by_symbol[symbol]
        window = candles.iloc[: index + 1].reset_index(drop=True)

        active_trade = active_trades.get(symbol)
        if active_trade is not None:
            price = float(window["close"].iloc[-1])
            if price >= active_trade["take_profit_price"] or price <= active_trade["safety_exit_price"]:
                trade_return_pct = ((price / active_trade["entry_price"]) - 1) * 100
                net_multiplier = 1 + ((trade_return_pct - fee_pct) / 100)
                cash_balance += trade_size * net_multiplier
                active_trades.pop(symbol, None)
            continue

        if cash_balance < trade_size:
            continue

        signal = strategy.analyze_entry(symbol, window)
        if signal.signal_type == "ENTER":
            cash_balance -= trade_size
            active_trades[symbol] = {
                "entry_price": signal.price,
                "take_profit_price": float(signal.take_profit_price),
                "safety_exit_price": float(signal.safety_exit_price),
            }
            max_concurrent = max(max_concurrent, len(active_trades))

    ending_equity = cash_balance
    for symbol, trade in active_trades.items():
        latest_price = float(candles_by_symbol[symbol]["close"].iloc[-1])
        unrealized_return_pct = ((latest_price / trade["entry_price"]) - 1) * 100
        ending_equity += trade_size * (1 + ((unrealized_return_pct - fee_pct) / 100))

    return {
        "ending_balance": round(cash_balance, 2),
        "ending_equity": round(ending_equity, 2),
        "portfolio_return_pct": round(((ending_equity / starting_balance) - 1) * 100, 2) if starting_balance else 0.0,
        "open_positions": len(active_trades),
        "max_concurrent_positions": max_concurrent,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a LOOP bot backtest report.")
    parser.add_argument("--exchange", default="kraken", help="Live exchange universe to validate symbols against.")
    parser.add_argument("--history-exchange", default=None, help="Optional history source exchange id for deeper candle data.")
    parser.add_argument("--days", type=int, default=60, help="Number of days of 15m candles to backtest.")
    parser.add_argument("--fee-pct", type=float, default=0.2, help="Round-trip fee assumption in percent.")
    parser.add_argument("--preset", default="short", choices=sorted(PRESET_OVERRIDES.keys()), help="Strategy preset to test.")
    parser.add_argument("--cache-dir", default="data/backtests", help="Folder for cached public candle data.")
    parser.add_argument("--starting-balance", type=float, default=10000.0, help="Starting cash balance for portfolio simulation.")
    parser.add_argument("--trade-size", type=float, default=None, help="Fixed dollar size per trade. Defaults to loop quote amount.")
    args = parser.parse_args()

    summary, results = run_backtest(
        exchange_id=args.exchange,
        days=args.days,
        fee_pct=args.fee_pct,
        preset=args.preset,
        cache_dir=Path(args.cache_dir),
        starting_balance=args.starting_balance,
        trade_size=args.trade_size,
        history_exchange_id=args.history_exchange,
    )
    print("SUMMARY")
    for key, value in summary.items():
        print(f"{key}={value}")
    print("RESULTS")
    for row in results:
        if row["status"] != "ok":
            print(f"{row['symbol']}|{row['status']}")
            continue
        print(
            "|".join(
                str(row[key])
                for key in [
                    "symbol",
                    "candles",
                    "trades",
                    "wins",
                    "losses",
                    "win_rate_pct",
                    "gross_return_pct",
                    "net_return_pct",
                    "avg_hold_hours",
                ]
            )
        )


if __name__ == "__main__":
    main()
