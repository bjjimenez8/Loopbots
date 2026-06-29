from __future__ import annotations

import argparse
from copy import deepcopy
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import ccxt
import pandas as pd

from market_regime import mode_allowed
from strategy import LoopStrategy, Signal


PreparedStrategy = tuple[dict[str, Any], LoopStrategy, pd.DataFrame, dict[int, int]]


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
    "strategy_modes": [
        {
            "name": "short",
            "market_type": "sideways",
            "allowed_base_assets": ["DOGE", "LINK", "SOL"],
            "market_type_rules": {
                "lookback": 96,
                "min_range_width_pct": 3.0,
                "max_range_width_pct": 9.0,
                "max_ema_slope_pct": 0.7,
                "min_support_touches": 4,
                "min_resistance_touches": 4,
                "min_range_position": 0.2,
                "max_range_position": 0.6,
            },
            "strategy_overrides": {
                "pullback_lookback": 5,
                "pullback_max_pct": 0.028,
                "bounce_confirmation_pct": 0.0012,
                "min_volume_ratio": 0.8,
                "max_active_minutes": 240,
            },
            "loop_settings": {
                "preset_name": "Short-term",
                "order_distance_pct": 1.0,
                "order_count": 10,
            },
        },
        {
            "name": "mid",
            "market_type": "any",
            "strategy_overrides": {
                "pullback_lookback": 6,
                "pullback_max_pct": 0.035,
                "bounce_confirmation_pct": 0.0015,
                "min_volume_ratio": 0.85,
                "max_active_minutes": 180,
            },
            "loop_settings": {
                "preset_name": "Mid-term",
                "order_distance_pct": 1.5,
                "order_count": 10,
            },
        },
    ],
}

PRESET_OVERRIDES: dict[str, dict[str, Any]] = {
    "dual": {},
    "sideways": {
        "strategy": {
            "entry_style": "sideways_accumulation",
            "pullback_lookback": 48,
            "bounce_confirmation_pct": 0.001,
            "min_signal_score": 78,
            "recent_drop_lookback": 96,
            "max_recent_drop_pct": 14.0,
            "sideways_min_range_width_pct": 6.0,
            "sideways_max_range_width_pct": 24.0,
            "sideways_max_ema_slope_pct": 5.0,
            "sideways_min_range_position": 0.18,
            "sideways_max_range_position": 0.62,
            "sideways_min_support_touches": 2,
            "sideways_min_resistance_touches": 1,
            "sideways_min_rsi": 35,
            "sideways_max_rsi": 64,
            "sideways_min_volume_ratio": 0.65,
        },
        "loop_settings": {
            "preset_name": "Sideways accumulation",
            "order_distance_pct": 2.0,
            "order_count": 10,
        },
    },
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

_EXCHANGE_CACHE: dict[tuple[str, bool], Any] = {}


def _safe_symbol(symbol: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", symbol).strip("_")


def _timeframe_minutes(timeframe: str) -> int:
    match = re.fullmatch(r"(\d+)([mhdw])", timeframe)
    if not match:
        return 15

    amount = int(match.group(1))
    unit = match.group(2)
    multipliers = {
        "m": 1,
        "h": 60,
        "d": 60 * 24,
        "w": 60 * 24 * 7,
    }
    return amount * multipliers[unit]


def _exchange(exchange_id: str, enable_rate_limit: bool) -> Any:
    cache_key = (exchange_id, enable_rate_limit)
    if cache_key not in _EXCHANGE_CACHE:
        exchange_class = getattr(ccxt, exchange_id)
        exchange = exchange_class({"enableRateLimit": enable_rate_limit})
        exchange.load_markets()
        _EXCHANGE_CACHE[cache_key] = exchange
    return _EXCHANGE_CACHE[cache_key]


def _coalesce_number(*values: Any) -> float:
    for value in values:
        try:
            if value is None:
                continue
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def _quote_volume(ticker: dict[str, Any], last_price: float) -> float:
    quote_volume = _coalesce_number(ticker.get("quoteVolume"))
    if quote_volume > 0:
        return quote_volume

    base_volume = _coalesce_number(ticker.get("baseVolume"))
    return base_volume * last_price if last_price > 0 else 0.0


def discover_backtest_pairs(exchange_id: str, config: dict[str, Any], max_pairs: int | None = None) -> list[str]:
    optimizer = config.get("optimizer", {})
    discovery = {**config.get("pair_discovery", {}), **optimizer}
    exchange = _exchange(exchange_id, config["exchange"].get("enable_rate_limit", True))
    quote_asset = discovery.get("quote_asset", "USDT")
    min_quote_volume = float(discovery.get("min_quote_volume_usdt", 1_250_000))
    min_last_price = float(discovery.get("min_last_price", 0.05))
    min_volatility_pct = float(discovery.get("min_volatility_pct", 1.8))
    max_volatility_pct = float(discovery.get("max_volatility_pct", 18.0))
    max_selected = int(max_pairs or discovery.get("max_pairs", 30))
    excluded_bases = {base.upper() for base in discovery.get("excluded_base_assets", [])}
    tickers = exchange.fetch_tickers()

    candidates: list[dict[str, Any]] = []
    for symbol, market in exchange.markets.items():
        if market.get("quote") != quote_asset:
            continue
        if not market.get("spot", False) or not market.get("active", True):
            continue

        base_asset = str(market.get("base", "")).upper()
        if not base_asset or base_asset in excluded_bases or base_asset.endswith(".S"):
            continue

        ticker = tickers.get(symbol) or {}
        last_price = _coalesce_number(ticker.get("last"), ticker.get("close"), 0.0)
        high_price = _coalesce_number(ticker.get("high"), last_price, 0.0)
        low_price = _coalesce_number(ticker.get("low"), last_price, 0.0)
        quote_volume = _quote_volume(ticker, last_price)
        volatility_pct = ((high_price - low_price) / low_price) * 100 if low_price > 0 else 0.0

        if quote_volume < min_quote_volume:
            continue
        if last_price < min_last_price or low_price <= 0:
            continue
        if volatility_pct < min_volatility_pct or volatility_pct > max_volatility_pct:
            continue

        candidates.append(
            {
                "symbol": symbol,
                "quote_volume": quote_volume,
                "volatility_pct": volatility_pct,
            }
        )

    candidates.sort(key=lambda item: (-item["quote_volume"], -item["volatility_pct"], item["symbol"]))
    return [item["symbol"] for item in candidates[:max_selected]]


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
            "strategy_modes": loaded.get("strategy_modes", DEFAULT_CONFIG["strategy_modes"]),
            "pair_discovery": loaded.get("pair_discovery", {}),
            "optimizer": loaded.get("optimizer", {}),
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
        "strategy_modes": list(config.get("strategy_modes", [])),
        "pair_discovery": dict(config.get("pair_discovery", {})),
        "optimizer": dict(config.get("optimizer", {})),
    }
    if "strategy" in overrides:
        merged["strategy"].update(overrides["strategy"])
    if "loop_settings" in overrides:
        merged["loop_settings"].update(overrides["loop_settings"])
    return merged


def fetch_candles(
    exchange: Any | None,
    symbol: str,
    timeframe: str,
    days: int,
    limit: int = 300,
    cache_dir: Path | None = None,
    exchange_id: str | None = None,
) -> pd.DataFrame:
    resolved_exchange_id = exchange.id if exchange is not None else exchange_id
    if not resolved_exchange_id:
        raise ValueError("exchange_id is required when exchange is not provided")

    cache_path = None
    expected_rows = max(int((days * 24 * 60) / _timeframe_minutes(timeframe)), 1)
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_name = f"{resolved_exchange_id}_{_safe_symbol(symbol)}_{timeframe}_{days}d.csv"
        cache_path = cache_dir / cache_name
        if cache_path.exists():
            cached = pd.read_csv(cache_path)
            for column in ["open", "high", "low", "close", "volume"]:
                cached[column] = pd.to_numeric(cached[column], errors="coerce")
            cached = cached.dropna().reset_index(drop=True)
            if len(cached) >= int(expected_rows * 0.98):
                return cached

    if exchange is None:
        exchange = _exchange(resolved_exchange_id, True)

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
        if len(rows) >= expected_rows + limit:
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
    validate_markets: bool = True,
    all_usdt_pairs: bool = False,
    max_pairs: int | None = None,
    loop_distances: list[float] | None = None,
    timeframe: str | None = None,
    order_count: int | None = None,
    symbols: list[str] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    config = apply_preset(load_public_config(), preset)
    config["exchange"]["id"] = exchange_id
    if timeframe:
        config["exchange"]["timeframe"] = timeframe
    if symbols:
        config["pairs"] = symbols
    if all_usdt_pairs:
        config["pairs"] = discover_backtest_pairs(exchange_id, config, max_pairs=max_pairs)

    history_exchange_name = history_exchange_id or exchange_id
    live_exchange = _exchange(exchange_id, config["exchange"]["enable_rate_limit"]) if validate_markets else None
    history_exchange = (
        _exchange(history_exchange_name, config["exchange"]["enable_rate_limit"]) if validate_markets else None
    )
    strategies = _build_strategy_modes(config, preset, loop_distances=loop_distances, order_count=order_count)
    fixed_trade_size = float(trade_size if trade_size is not None else config["loop_settings"]["quote_amount_usdt"])
    mode_results = {
        mode["name"]: {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "profitable_trades": 0,
            "net_return_pct": 0.0,
            "grid_net_return_pct": 0.0,
            "grid_cycles": 0,
        }
        for mode, _ in strategies
    }

    summary = {
        "exchange": exchange_id,
        "history_exchange": history_exchange_name,
        "preset": preset,
        "days": days,
        "timeframe": config["exchange"]["timeframe"],
        "fee_pct": fee_pct,
        "starting_balance": starting_balance,
        "trade_size": fixed_trade_size,
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "profitable_trades": 0,
        "price_gross_return_pct": 0.0,
        "price_net_return_pct": 0.0,
        "grid_gross_return_pct": 0.0,
        "grid_net_return_pct": 0.0,
        "grid_cycles": 0,
        "gross_return_pct": 0.0,
        "net_return_pct": 0.0,
    }
    results: list[dict[str, Any]] = []
    candles_by_symbol: dict[str, pd.DataFrame] = {}

    for symbol in config["pairs"]:
        if validate_markets and live_exchange is not None and symbol not in live_exchange.markets:
            results.append({"symbol": symbol, "status": "missing"})
            continue
        if validate_markets and history_exchange is not None and symbol not in history_exchange.markets:
            results.append({"symbol": symbol, "status": "missing_history"})
            continue

        try:
            candles = fetch_candles(
                history_exchange,
                symbol,
                config["exchange"]["timeframe"],
                days,
                cache_dir=cache_dir,
                exchange_id=history_exchange_name,
            )
        except Exception as exc:
            results.append({"symbol": symbol, "status": f"history_error:{exc.__class__.__name__}"})
            continue
        candles_by_symbol[symbol] = candles
        prepared_strategies = _prepare_strategy_frames(candles, strategies)
        active_trade: dict[str, Any] | None = None
        wins = 0
        losses = 0
        profitable_trades = 0
        price_gross_return_pct = 0.0
        price_net_return_pct = 0.0
        grid_gross_return_pct = 0.0
        grid_net_return_pct = 0.0
        grid_cycles = 0
        gross_return_pct = 0.0
        net_return_pct = 0.0
        hold_bars: list[int] = []

        min_candles = max(strategy._minimum_candles for _, strategy in strategies)
        mode_entries = {mode["name"]: 0 for mode, _ in strategies}
        mode_wins = {mode["name"]: 0 for mode, _ in strategies}
        mode_losses = {mode["name"]: 0 for mode, _ in strategies}
        mode_profitable = {mode["name"]: 0 for mode, _ in strategies}
        mode_grid_net = {mode["name"]: 0.0 for mode, _ in strategies}
        mode_grid_cycles = {mode["name"]: 0 for mode, _ in strategies}

        for index in range(min_candles, len(candles)):
            window = candles.iloc[: index + 1].reset_index(drop=True)

            if active_trade is None:
                mode_name, signal = _analyze_entry_fast(symbol, candles, index, prepared_strategies)
                if signal.signal_type == "ENTER":
                    active_trade = {
                        "mode": mode_name,
                        "entry_price": signal.price,
                        "take_profit_price": float(signal.take_profit_price),
                        "safety_exit_price": float(signal.safety_exit_price),
                        "entry_index": index,
                        "grid": _new_grid_state(signal, fee_pct),
                    }
                    mode_entries[mode_name] += 1
                continue

            _update_grid_state(active_trade["grid"], candles.iloc[index])
            price = float(window["close"].iloc[-1])
            price_return_pct = ((price / active_trade["entry_price"]) - 1) * 100
            grid_gross_pct = active_trade["grid"]["gross_return_pct"]
            grid_net_pct = active_trade["grid"]["net_return_pct"]
            trade_gross_return_pct = price_return_pct + grid_gross_pct
            trade_net_return_pct = price_return_pct - fee_pct + grid_net_pct
            if price >= active_trade["take_profit_price"]:
                wins += 1
                profitable_trades += int(trade_net_return_pct > 0)
                price_gross_return_pct += price_return_pct
                price_net_return_pct += price_return_pct - fee_pct
                grid_gross_return_pct += grid_gross_pct
                grid_net_return_pct += grid_net_pct
                grid_cycles += active_trade["grid"]["cycles"]
                gross_return_pct += trade_gross_return_pct
                net_return_pct += trade_net_return_pct
                mode_wins[active_trade["mode"]] += 1
                mode_profitable[active_trade["mode"]] += int(trade_net_return_pct > 0)
                mode_grid_net[active_trade["mode"]] += grid_net_pct
                mode_grid_cycles[active_trade["mode"]] += active_trade["grid"]["cycles"]
                mode_results[active_trade["mode"]]["net_return_pct"] += trade_net_return_pct
                hold_bars.append(index - active_trade["entry_index"])
                active_trade = None
            elif price <= active_trade["safety_exit_price"]:
                losses += 1
                profitable_trades += int(trade_net_return_pct > 0)
                price_gross_return_pct += price_return_pct
                price_net_return_pct += price_return_pct - fee_pct
                grid_gross_return_pct += grid_gross_pct
                grid_net_return_pct += grid_net_pct
                grid_cycles += active_trade["grid"]["cycles"]
                gross_return_pct += trade_gross_return_pct
                net_return_pct += trade_net_return_pct
                mode_losses[active_trade["mode"]] += 1
                mode_profitable[active_trade["mode"]] += int(trade_net_return_pct > 0)
                mode_grid_net[active_trade["mode"]] += grid_net_pct
                mode_grid_cycles[active_trade["mode"]] += active_trade["grid"]["cycles"]
                mode_results[active_trade["mode"]]["net_return_pct"] += trade_net_return_pct
                hold_bars.append(index - active_trade["entry_index"])
                active_trade = None

        trades = wins + losses
        monthly_profit_estimate = (fixed_trade_size * (net_return_pct / 100)) / max(days / 30, 1)
        results.append(
            {
                "symbol": symbol,
                "status": "ok",
                "candles": len(candles),
                "trades": trades,
                "wins": wins,
                "losses": losses,
                "profitable_trades": profitable_trades,
                "win_rate_pct": round((wins / trades * 100), 2) if trades else 0.0,
                "profit_win_rate_pct": round((profitable_trades / trades * 100), 2) if trades else 0.0,
                "price_gross_return_pct": round(price_gross_return_pct, 2),
                "price_net_return_pct": round(price_net_return_pct, 2),
                "grid_gross_return_pct": round(grid_gross_return_pct, 2),
                "grid_net_return_pct": round(grid_net_return_pct, 2),
                "grid_cycles": grid_cycles,
                "gross_return_pct": round(gross_return_pct, 2),
                "net_return_pct": round(net_return_pct, 2),
                "monthly_profit_estimate": round(monthly_profit_estimate, 2),
                "monthly_return_on_trade_size_pct": round((net_return_pct / max(days / 30, 1)), 2),
                "avg_hold_hours": round((sum(hold_bars) / len(hold_bars) * 0.25), 2) if hold_bars else 0.0,
                "entries_by_mode": mode_entries,
            }
        )
        for mode_name in mode_results:
            mode_results[mode_name]["trades"] += mode_wins[mode_name] + mode_losses[mode_name]
            mode_results[mode_name]["wins"] += mode_wins[mode_name]
            mode_results[mode_name]["losses"] += mode_losses[mode_name]
            mode_results[mode_name]["profitable_trades"] += mode_profitable[mode_name]
            mode_results[mode_name]["grid_net_return_pct"] += mode_grid_net[mode_name]
            mode_results[mode_name]["grid_cycles"] += mode_grid_cycles[mode_name]
        summary["trades"] += trades
        summary["wins"] += wins
        summary["losses"] += losses
        summary["profitable_trades"] += profitable_trades
        summary["price_gross_return_pct"] += price_gross_return_pct
        summary["price_net_return_pct"] += price_net_return_pct
        summary["grid_gross_return_pct"] += grid_gross_return_pct
        summary["grid_net_return_pct"] += grid_net_return_pct
        summary["grid_cycles"] += grid_cycles
        summary["gross_return_pct"] += gross_return_pct
        summary["net_return_pct"] += net_return_pct

    summary["price_gross_return_pct"] = round(summary["price_gross_return_pct"], 2)
    summary["price_net_return_pct"] = round(summary["price_net_return_pct"], 2)
    summary["grid_gross_return_pct"] = round(summary["grid_gross_return_pct"], 2)
    summary["grid_net_return_pct"] = round(summary["grid_net_return_pct"], 2)
    summary["gross_return_pct"] = round(summary["gross_return_pct"], 2)
    summary["net_return_pct"] = round(summary["net_return_pct"], 2)
    summary["win_rate_pct"] = round((summary["wins"] / summary["trades"] * 100), 2) if summary["trades"] else 0.0
    summary["profit_win_rate_pct"] = (
        round((summary["profitable_trades"] / summary["trades"] * 100), 2) if summary["trades"] else 0.0
    )
    summary["avg_grid_cycles_per_trade"] = (
        round(summary["grid_cycles"] / summary["trades"], 2) if summary["trades"] else 0.0
    )
    for mode_name, mode_result in mode_results.items():
        trades = mode_result["trades"]
        mode_result["win_rate_pct"] = round((mode_result["wins"] / trades * 100), 2) if trades else 0.0
        mode_result["profit_win_rate_pct"] = (
            round((mode_result["profitable_trades"] / trades * 100), 2) if trades else 0.0
        )
        mode_result["net_return_pct"] = round(mode_result["net_return_pct"], 2)
        mode_result["grid_net_return_pct"] = round(mode_result["grid_net_return_pct"], 2)
    summary["mode_results"] = mode_results
    balance_summary = simulate_portfolio(
        candles_by_symbol=candles_by_symbol,
        strategies=strategies,
        fee_pct=fee_pct,
        starting_balance=starting_balance,
        trade_size=fixed_trade_size,
    )
    summary.update(balance_summary)
    summary["allocation"] = build_allocation_plan(
        results=results,
        starting_balance=starting_balance,
        max_active_bots=int(config.get("optimizer", {}).get("max_active_bots", 4)),
        max_deployed_pct=float(config.get("optimizer", {}).get("max_deployed_pct", 60)),
        max_coin_allocation_pct=float(config.get("optimizer", {}).get("max_coin_allocation_pct", 15)),
        min_trades=int(config.get("optimizer", {}).get("min_allocation_trades", 3)),
    )
    return summary, results


def _build_strategy_modes(
    config: dict[str, Any],
    preset: str,
    loop_distances: list[float] | None = None,
    order_count: int | None = None,
) -> list[tuple[dict[str, Any], LoopStrategy]]:
    if loop_distances:
        resolved_order_count = int(order_count or config.get("optimizer", {}).get("order_count", config["loop_settings"].get("order_count", 10)))
        modes = [
            {
                "name": f"loop_{str(distance).replace('.', '_')}",
                "market_type": "any",
                "strategy_overrides": {},
                "loop_settings": {
                    "preset_name": f"LOOP {distance}%",
                    "order_distance_pct": distance,
                    "order_count": resolved_order_count,
                },
            }
            for distance in loop_distances
        ]
    elif preset == "dual":
        modes = config.get("strategy_modes") or DEFAULT_CONFIG["strategy_modes"]
    else:
        mode_name = preset
        strategy_config = deepcopy(config["strategy"])
        loop_settings = deepcopy(config["loop_settings"])
        overrides = PRESET_OVERRIDES.get(preset, {})
        strategy_config.update(overrides.get("strategy", {}))
        loop_settings.update(overrides.get("loop_settings", {}))
        return [
            (
                {"name": mode_name, "market_type": "any"},
                LoopStrategy(strategy_config, loop_settings),
            )
        ]

    strategies: list[tuple[dict[str, Any], LoopStrategy]] = []
    for mode in modes:
        strategy_config = deepcopy(config["strategy"])
        strategy_config.update(mode.get("strategy_overrides", {}))
        loop_settings = deepcopy(config["loop_settings"])
        loop_settings.update(mode.get("loop_settings", {}))
        strategies.append((mode, LoopStrategy(strategy_config, loop_settings)))
    return strategies


def _analyze_entry(
    symbol: str,
    candles: pd.DataFrame,
    strategies: list[tuple[dict[str, Any], LoopStrategy]],
) -> tuple[str, Signal]:
    entry_candidates: list[tuple[str, Signal]] = []
    for mode, strategy in strategies:
        if not mode_allowed(mode, candles, symbol):
            continue
        signal = strategy.analyze_entry(symbol, candles)
        if signal.signal_type == "ENTER":
            entry_candidates.append((mode["name"], signal))
    if entry_candidates:
        return max(entry_candidates, key=lambda item: _entry_score(item[1]))
    return "", Signal("HOLD", symbol=symbol, price=float(candles["close"].iloc[-1]), reason="no entry setup")


def _prepare_strategy_frames(
    candles: pd.DataFrame,
    strategies: list[tuple[dict[str, Any], LoopStrategy]],
) -> list[PreparedStrategy]:
    source = candles.reset_index().rename(columns={"index": "source_index"})
    prepared: list[PreparedStrategy] = []
    for mode, strategy in strategies:
        indicator_frame = strategy._with_indicators(source)
        positions = {
            int(row.source_index): int(row.Index)
            for row in indicator_frame[["source_index"]].itertuples()
        }
        prepared.append((mode, strategy, indicator_frame, positions))
    return prepared


def _analyze_entry_fast(
    symbol: str,
    candles: pd.DataFrame,
    current_index: int,
    prepared_strategies: list[PreparedStrategy],
) -> tuple[str, Signal]:
    raw_window = candles.iloc[: current_index + 1].reset_index(drop=True)
    entry_candidates: list[tuple[str, Signal]] = []
    for mode, strategy, indicator_frame, positions in prepared_strategies:
        prepared_index = positions.get(current_index)
        if prepared_index is None or prepared_index < max(strategy.config["pullback_lookback"], strategy._range_lookback, 2):
            continue
        if not mode_allowed(mode, raw_window, symbol):
            continue
        signal = _signal_from_indicators(strategy, symbol, indicator_frame.iloc[: prepared_index + 1])
        if signal.signal_type == "ENTER":
            entry_candidates.append((mode["name"], signal))
    if entry_candidates:
        return max(entry_candidates, key=lambda item: _entry_score(item[1]))
    return "", Signal("HOLD", symbol=symbol, price=float(candles["close"].iloc[current_index]), reason="no entry setup")


def _entry_score(signal: Signal) -> tuple[float, float, float]:
    loop_plan = (signal.loop_settings or {}).get("loop_plan", {})
    setup_score = float(loop_plan.get("setup_score") or 0.0)
    reward_to_risk = float(loop_plan.get("reward_to_risk") or 0.0)
    order_distance_pct = float(loop_plan.get("order_distance_pct") or 0.0)
    return setup_score, reward_to_risk, order_distance_pct


def _signal_from_indicators(strategy: LoopStrategy, symbol: str, df: pd.DataFrame) -> Signal:
    if strategy.config.get("entry_style") == "sideways_accumulation":
        return _sideways_signal_from_indicators(strategy, symbol, df)

    profile = strategy._symbol_profile(symbol)
    latest = df.iloc[-1]
    previous = df.iloc[-2]
    recent = df.iloc[-strategy.config["pullback_lookback"] :]
    range_window = df.iloc[-strategy._range_lookback :]
    price = float(latest["close"])
    atr = float(latest["atr"])
    range_low = float(range_window["low"].min())
    range_high = float(range_window["high"].max())
    range_span = max(range_high - range_low, 0.0)
    range_position = ((price - range_low) / range_span) if range_span else 1.0

    trend_ok = (
        latest["ema_fast"] > latest["ema_slow"] > latest["ema_trend"]
        and latest["ema_trend"] > previous["ema_trend"]
    )
    price_reclaimed_fast_ema = latest["close"] > latest["ema_fast"] * profile["ema_reclaim_buffer"]
    recent_high = float(recent["high"].max())
    pullback_pct = (recent_high - price) / recent_high if recent_high else 0.0
    pullback_ok = strategy.config["pullback_min_pct"] <= pullback_pct <= strategy.config["pullback_max_pct"]
    bounce_ok = (
        latest["close"] >= latest["low"] * (1 + (strategy.config["bounce_confirmation_pct"] * profile["bounce_multiplier"]))
        and latest["close"] >= previous["close"] * profile["previous_close_buffer"]
        and latest["close"] >= latest["open"] * profile["open_buffer"]
    )
    rsi_ok = (
        (strategy.config["min_rsi"] - profile["rsi_low_buffer"])
        <= latest["rsi"]
        <= (strategy.config["max_rsi"] + profile["rsi_high_buffer"])
    )
    volume_ok = latest["volume_ratio"] >= max(strategy.config["min_volume_ratio"] - profile["volume_buffer"], 0.6)
    breakdown_ok = strategy._breakdown_ok(df)
    loop_plan = strategy._build_loop_plan(range_window, price, atr)
    loop_ready = bool(loop_plan) and strategy._loop_ready(loop_plan, price, range_position, profile)
    setup_score = strategy._setup_score(
        latest=latest,
        trend_ok=trend_ok,
        price_reclaimed_fast_ema=price_reclaimed_fast_ema,
        pullback_ok=pullback_ok,
        bounce_ok=bounce_ok,
        rsi_ok=rsi_ok,
        volume_ok=volume_ok,
        loop_plan=loop_plan,
        range_position=range_position,
    )
    min_signal_score = float(strategy.config.get("min_signal_score", 0.0))

    if (
        trend_ok
        and price_reclaimed_fast_ema
        and pullback_ok
        and bounce_ok
        and rsi_ok
        and volume_ok
        and breakdown_ok
        and loop_ready
        and setup_score >= min_signal_score
    ):
        assert loop_plan is not None
        loop_plan["setup_score"] = setup_score
        return Signal(
            "ENTER",
            symbol=symbol,
            price=price,
            take_profit_price=loop_plan["take_profit_price"],
            safety_exit_price=loop_plan["safety_exit_price"],
            reason="Trend up, shallow pullback, bounce confirmed",
            loop_settings={
                **strategy.loop_settings,
                "loop_plan": loop_plan,
            },
        )

    return Signal("HOLD", symbol=symbol, price=price, reason="no entry setup")


def _sideways_signal_from_indicators(strategy: LoopStrategy, symbol: str, df: pd.DataFrame) -> Signal:
    latest = df.iloc[-1]
    previous = df.iloc[-2]
    range_window = df.iloc[-strategy._range_lookback :]
    price = float(latest["close"])
    atr = float(latest["atr"])
    range_low = float(range_window["low"].min())
    range_high = float(range_window["high"].max())
    range_span = range_high - range_low
    if price <= 0 or range_span <= 0:
        return Signal("HOLD", symbol=symbol, price=price, reason="invalid range")

    range_width_pct = (range_span / price) * 100
    range_position = (price - range_low) / range_span
    ema_slope_lookback = min(24, max(3, len(df) - 1))
    ema_slope_pct = abs((float(latest["ema_trend"]) / float(df["ema_trend"].iloc[-ema_slope_lookback]) - 1) * 100)
    support_level = range_low + (range_span * 0.25)
    resistance_level = range_high - (range_span * 0.25)
    support_touches = int((range_window["low"] <= support_level).sum())
    resistance_touches = int((range_window["high"] >= resistance_level).sum())

    range_ok = (
        float(strategy.config.get("sideways_min_range_width_pct", 5.0))
        <= range_width_pct
        <= float(strategy.config.get("sideways_max_range_width_pct", 22.0))
    )
    slope_ok = ema_slope_pct <= float(strategy.config.get("sideways_max_ema_slope_pct", 4.0))
    position_ok = (
        float(strategy.config.get("sideways_min_range_position", 0.15))
        <= range_position
        <= float(strategy.config.get("sideways_max_range_position", 0.62))
    )
    touches_ok = support_touches >= int(strategy.config.get("sideways_min_support_touches", 2)) and resistance_touches >= int(
        strategy.config.get("sideways_min_resistance_touches", 1)
    )
    bounce_ok = latest["close"] >= latest["low"] * (1 + strategy.config["bounce_confirmation_pct"]) and latest["close"] >= previous["close"] * 0.995
    rsi_ok = float(strategy.config.get("sideways_min_rsi", 35)) <= latest["rsi"] <= float(strategy.config.get("sideways_max_rsi", 62))
    volume_ok = latest["volume_ratio"] >= float(strategy.config.get("sideways_min_volume_ratio", 0.7))
    breakdown_ok = strategy._breakdown_ok(df)
    loop_plan = strategy._build_loop_plan(range_window, price, atr)
    profile = strategy._symbol_profile(symbol)
    loop_ready = bool(loop_plan) and strategy._loop_ready(loop_plan, price, range_position, profile)
    setup_score = strategy._sideways_setup_score(
        range_ok=range_ok,
        slope_ok=slope_ok,
        position_ok=position_ok,
        touches_ok=touches_ok,
        bounce_ok=bounce_ok,
        rsi_ok=rsi_ok,
        volume_ok=volume_ok,
        breakdown_ok=breakdown_ok,
        loop_plan=loop_plan,
        range_position=range_position,
        range_width_pct=range_width_pct,
    )
    min_signal_score = float(strategy.config.get("min_signal_score", 0.0))

    if (
        range_ok
        and slope_ok
        and position_ok
        and touches_ok
        and bounce_ok
        and rsi_ok
        and volume_ok
        and breakdown_ok
        and loop_ready
        and setup_score >= min_signal_score
    ):
        assert loop_plan is not None
        loop_plan["setup_score"] = setup_score
        loop_plan["range_position"] = round(range_position, 2)
        return Signal(
            "ENTER",
            symbol=symbol,
            price=price,
            take_profit_price=loop_plan["take_profit_price"],
            safety_exit_price=loop_plan["safety_exit_price"],
            reason="Sideways range, lower-half entry, bounce confirmed",
            loop_settings={
                **strategy.loop_settings,
                "loop_plan": loop_plan,
            },
        )

    return Signal("HOLD", symbol=symbol, price=price, reason="no sideways setup")


def _new_grid_state(signal: Signal, fee_pct: float) -> dict[str, Any]:
    loop_settings = signal.loop_settings or {}
    loop_plan = loop_settings.get("loop_plan", {})
    order_distance_pct = float(
        loop_plan.get("order_distance_pct")
        or loop_settings.get("order_distance_pct")
        or 1.5
    )
    order_count = int(loop_plan.get("order_count") or loop_settings.get("order_count") or 10)
    step = order_distance_pct / 100
    max_pending_orders = max(order_count // 2, 1)

    return {
        "entry_price": float(signal.price),
        "order_distance_pct": order_distance_pct,
        "order_count": order_count,
        "step": step,
        "fee_pct": fee_pct,
        "max_pending_orders": max_pending_orders,
        "pending_levels": [],
        "cycles": 0,
        "gross_return_pct": 0.0,
        "net_return_pct": 0.0,
    }


def _update_grid_state(grid_state: dict[str, Any], candle: pd.Series) -> None:
    for price in _candle_path(candle):
        _process_grid_price(grid_state, price)


def _candle_path(candle: pd.Series) -> list[float]:
    open_price = float(candle["open"])
    high_price = float(candle["high"])
    low_price = float(candle["low"])
    close_price = float(candle["close"])
    if close_price >= open_price:
        return [open_price, low_price, high_price, close_price]
    return [open_price, high_price, low_price, close_price]


def _process_grid_price(grid_state: dict[str, Any], price: float) -> None:
    entry_price = float(grid_state["entry_price"])
    step = float(grid_state["step"])
    max_pending_orders = int(grid_state["max_pending_orders"])
    pending_levels = set(grid_state["pending_levels"])

    for level in range(1, max_pending_orders + 1):
        if level in pending_levels:
            continue
        buy_price = entry_price * ((1 - step) ** level)
        if price <= buy_price:
            pending_levels.add(level)

    for level in sorted(pending_levels):
        buy_price = entry_price * ((1 - step) ** level)
        sell_price = buy_price * (1 + step)
        if price >= sell_price:
            pending_levels.remove(level)
            grid_state["cycles"] += 1
            grid_state["gross_return_pct"] += grid_state["order_distance_pct"] / grid_state["order_count"]
            grid_state["net_return_pct"] += (
                grid_state["order_distance_pct"] - grid_state["fee_pct"]
            ) / grid_state["order_count"]

    grid_state["pending_levels"] = sorted(pending_levels)


def _combined_trade_returns(active_trade: dict[str, Any], price: float, fee_pct: float) -> dict[str, float]:
    price_gross_return_pct = ((price / active_trade["entry_price"]) - 1) * 100
    price_net_return_pct = price_gross_return_pct - fee_pct
    grid_gross_return_pct = active_trade["grid"]["gross_return_pct"]
    grid_net_return_pct = active_trade["grid"]["net_return_pct"]
    return {
        "price_gross_return_pct": price_gross_return_pct,
        "price_net_return_pct": price_net_return_pct,
        "grid_gross_return_pct": grid_gross_return_pct,
        "grid_net_return_pct": grid_net_return_pct,
        "gross_return_pct": price_gross_return_pct + grid_gross_return_pct,
        "net_return_pct": price_net_return_pct + grid_net_return_pct,
    }


def build_allocation_plan(
    results: list[dict[str, Any]],
    starting_balance: float,
    max_active_bots: int,
    max_deployed_pct: float,
    max_coin_allocation_pct: float,
    min_trades: int = 3,
) -> dict[str, Any]:
    ranked = [
        row
        for row in results
        if row.get("status") == "ok"
        and int(row.get("trades", 0)) >= min_trades
        and float(row.get("monthly_profit_estimate", 0.0)) > 0
    ]
    ranked.sort(
        key=lambda row: (
            -float(row.get("monthly_profit_estimate", 0.0)),
            -float(row.get("win_rate_pct", 0.0)),
            -int(row.get("trades", 0)),
        )
    )

    selected = ranked[: max(max_active_bots, 0)]
    max_deployed = starting_balance * (max_deployed_pct / 100)
    max_per_coin = starting_balance * (max_coin_allocation_pct / 100)
    allocation_per_bot = min(max_deployed / len(selected), max_per_coin) if selected else 0.0
    estimated_monthly_profit = sum(
        allocation_per_bot * (float(row.get("monthly_return_on_trade_size_pct", 0.0)) / 100)
        for row in selected
    )

    return {
        "max_active_bots": max_active_bots,
        "max_deployed": round(max_deployed, 2),
        "allocation_per_bot": round(allocation_per_bot, 2),
        "estimated_monthly_profit": round(estimated_monthly_profit, 2),
        "selected": [
            {
                "symbol": row["symbol"],
                "trades": row["trades"],
                "win_rate_pct": row["win_rate_pct"],
                "monthly_profit_estimate": row["monthly_profit_estimate"],
                "monthly_return_on_trade_size_pct": row["monthly_return_on_trade_size_pct"],
                "suggested_allocation": round(allocation_per_bot, 2),
                "estimated_profit_at_allocation": round(
                    allocation_per_bot * (float(row.get("monthly_return_on_trade_size_pct", 0.0)) / 100),
                    2,
                ),
            }
            for row in selected
        ],
    }


def _parse_float_list(value: str) -> list[float]:
    return [float(item.strip()) for item in value.split(",") if item.strip()]


def run_loop_optimizer(
    exchange_id: str,
    history_exchange_id: str | None,
    days: int,
    fee_pct: float,
    preset: str,
    cache_dir: Path | None,
    starting_balance: float,
    trade_size: float | None,
    validate_markets: bool,
    all_usdt_pairs: bool,
    max_pairs: int | None,
    loop_distances: list[float],
    timeframe: str | None = None,
    order_count: int | None = None,
    symbols: list[str] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    best_by_symbol: dict[str, dict[str, Any]] = {}
    distance_summaries: list[dict[str, Any]] = []

    for distance in loop_distances:
        summary, results = run_backtest(
            exchange_id=exchange_id,
            days=days,
            fee_pct=fee_pct,
            preset=preset,
            cache_dir=cache_dir,
            starting_balance=starting_balance,
            trade_size=trade_size,
            history_exchange_id=history_exchange_id,
            validate_markets=validate_markets,
            all_usdt_pairs=all_usdt_pairs,
            max_pairs=max_pairs,
            loop_distances=[distance],
            timeframe=timeframe,
            order_count=order_count,
            symbols=symbols,
        )
        distance_summaries.append(
            {
                "distance": distance,
                "trades": summary["trades"],
                "win_rate_pct": summary["win_rate_pct"],
                "portfolio_return_pct": summary["portfolio_return_pct"],
                "max_drawdown_pct": summary["max_drawdown_pct"],
                "estimated_monthly_profit": summary["allocation"]["estimated_monthly_profit"],
            }
        )
        for row in results:
            if row.get("status") != "ok":
                continue
            candidate = {**row, "best_distance_pct": distance}
            existing = best_by_symbol.get(row["symbol"])
            if existing is None or (
                float(candidate.get("monthly_profit_estimate", 0.0)),
                float(candidate.get("win_rate_pct", 0.0)),
                int(candidate.get("trades", 0)),
            ) > (
                float(existing.get("monthly_profit_estimate", 0.0)),
                float(existing.get("win_rate_pct", 0.0)),
                int(existing.get("trades", 0)),
            ):
                best_by_symbol[row["symbol"]] = candidate

    best_rows = sorted(
        best_by_symbol.values(),
        key=lambda row: (
            -float(row.get("monthly_profit_estimate", 0.0)),
            -float(row.get("win_rate_pct", 0.0)),
            -int(row.get("trades", 0)),
        ),
    )
    config = apply_preset(load_public_config(), preset)
    optimizer = config.get("optimizer", {})
    allocation = build_allocation_plan(
        results=best_rows,
        starting_balance=starting_balance,
        max_active_bots=int(optimizer.get("max_active_bots", 4)),
        max_deployed_pct=float(optimizer.get("max_deployed_pct", 60)),
        max_coin_allocation_pct=float(optimizer.get("max_coin_allocation_pct", 15)),
        min_trades=int(optimizer.get("min_allocation_trades", 3)),
    )
    return {
        "exchange": exchange_id,
        "history_exchange": history_exchange_id or exchange_id,
        "days": days,
        "timeframe": timeframe or config["exchange"]["timeframe"],
        "preset": preset,
        "fee_pct": fee_pct,
        "starting_balance": starting_balance,
        "trade_size": float(trade_size or config["loop_settings"]["quote_amount_usdt"]),
        "distances_tested": loop_distances,
        "order_count": int(order_count or config.get("optimizer", {}).get("order_count", config["loop_settings"].get("order_count", 10))),
        "distance_summaries": distance_summaries,
        "allocation": allocation,
    }, best_rows


def simulate_portfolio(
    candles_by_symbol: dict[str, pd.DataFrame],
    strategies: list[tuple[dict[str, Any], LoopStrategy]],
    fee_pct: float,
    starting_balance: float,
    trade_size: float,
) -> dict[str, Any]:
    cash_balance = float(starting_balance)
    active_trades: dict[str, dict[str, Any]] = {}
    timeline: list[tuple[int, str, int]] = []
    prepared_by_symbol = {
        symbol: _prepare_strategy_frames(candles, strategies)
        for symbol, candles in candles_by_symbol.items()
    }

    for symbol, candles in candles_by_symbol.items():
        min_candles = max(strategy._minimum_candles for _, strategy in strategies)
        for index in range(min_candles, len(candles)):
            timeline.append((int(candles.iloc[index]["timestamp"]), symbol, index))

    timeline.sort()
    max_concurrent = 0
    peak_equity = cash_balance
    max_drawdown_pct = 0.0
    monthly_pnl: dict[str, float] = {}
    current_losing_streak = 0
    max_losing_streak = 0
    closed_trade_returns: list[float] = []
    last_prices: dict[str, float] = {}

    def mark_to_market_equity() -> float:
        equity = cash_balance
        for active_symbol, trade in active_trades.items():
            mark_price = last_prices.get(active_symbol, float(trade["entry_price"]))
            returns = _combined_trade_returns(trade, mark_price, fee_pct)
            equity += trade_size * (1 + (returns["net_return_pct"] / 100))
        return equity

    for timestamp, symbol, index in timeline:
        candles = candles_by_symbol[symbol]
        window = candles.iloc[: index + 1].reset_index(drop=True)
        last_prices[symbol] = float(window["close"].iloc[-1])

        active_trade = active_trades.get(symbol)
        if active_trade is not None:
            _update_grid_state(active_trade["grid"], candles.iloc[index])
            price = float(window["close"].iloc[-1])
            if price >= active_trade["take_profit_price"] or price <= active_trade["safety_exit_price"]:
                returns = _combined_trade_returns(active_trade, price, fee_pct)
                trade_profit = trade_size * (returns["net_return_pct"] / 100)
                cash_balance += trade_size + trade_profit
                month_key = datetime.fromtimestamp(timestamp / 1000, UTC).strftime("%Y-%m")
                monthly_pnl[month_key] = monthly_pnl.get(month_key, 0.0) + trade_profit
                closed_trade_returns.append(returns["net_return_pct"])
                if returns["net_return_pct"] <= 0:
                    current_losing_streak += 1
                    max_losing_streak = max(max_losing_streak, current_losing_streak)
                else:
                    current_losing_streak = 0
                active_trades.pop(symbol, None)
        elif cash_balance >= trade_size:
            _, signal = _analyze_entry_fast(symbol, candles, index, prepared_by_symbol[symbol])
            if signal.signal_type == "ENTER":
                cash_balance -= trade_size
                active_trades[symbol] = {
                    "entry_price": signal.price,
                    "take_profit_price": float(signal.take_profit_price),
                    "safety_exit_price": float(signal.safety_exit_price),
                    "grid": _new_grid_state(signal, fee_pct),
                }
                max_concurrent = max(max_concurrent, len(active_trades))

        equity = mark_to_market_equity()
        peak_equity = max(peak_equity, equity)
        if peak_equity > 0:
            max_drawdown_pct = min(max_drawdown_pct, ((equity / peak_equity) - 1) * 100)

    ending_equity = mark_to_market_equity()
    monthly_returns = [
        {
            "month": month,
            "profit": round(profit, 2),
            "return_pct": round((profit / starting_balance) * 100, 2) if starting_balance else 0.0,
        }
        for month, profit in sorted(monthly_pnl.items())
    ]
    worst_month = min(monthly_returns, key=lambda row: row["profit"], default={})
    best_month = max(monthly_returns, key=lambda row: row["profit"], default={})

    return {
        "ending_balance": round(cash_balance, 2),
        "ending_equity": round(ending_equity, 2),
        "portfolio_return_pct": round(((ending_equity / starting_balance) - 1) * 100, 2) if starting_balance else 0.0,
        "open_positions": len(active_trades),
        "max_concurrent_positions": max_concurrent,
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "avg_trade_net_return_pct": (
            round(sum(closed_trade_returns) / len(closed_trade_returns), 2) if closed_trade_returns else 0.0
        ),
        "max_losing_streak": max_losing_streak,
        "best_month": best_month,
        "worst_month": worst_month,
        "monthly_returns": monthly_returns,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a LOOP bot backtest report.")
    parser.add_argument("--exchange", default="kraken", help="Live exchange universe to validate symbols against.")
    parser.add_argument("--history-exchange", default=None, help="Optional history source exchange id for deeper candle data.")
    parser.add_argument("--days", type=int, default=60, help="Number of days of candles to backtest.")
    parser.add_argument("--timeframe", default=None, help="Override candle timeframe, for example 15m, 30m, or 1h.")
    parser.add_argument("--fee-pct", type=float, default=0.2, help="Round-trip fee assumption in percent.")
    parser.add_argument("--preset", default="dual", choices=sorted(PRESET_OVERRIDES.keys()), help="Strategy preset to test.")
    parser.add_argument("--cache-dir", default="data/backtests", help="Folder for cached public candle data.")
    parser.add_argument("--starting-balance", type=float, default=10000.0, help="Starting cash balance for portfolio simulation.")
    parser.add_argument("--trade-size", type=float, default=None, help="Fixed dollar size per trade. Defaults to loop quote amount.")
    parser.add_argument("--symbols", default="", help="Comma-separated symbols to test, such as DOGE/USDT,SOL/USDT.")
    parser.add_argument("--all-usdt-pairs", action="store_true", help="Discover and test liquid spot USDT pairs from the live exchange.")
    parser.add_argument("--max-pairs", type=int, default=None, help="Maximum discovered USDT pairs to test.")
    parser.add_argument("--order-count", type=int, default=None, help="Override LOOP order count. Bitsgap-style valid range is 10-40 even numbers.")
    parser.add_argument(
        "--loop-distances",
        default=None,
        help="Comma-separated LOOP order distances to test, for example 0.8,1.0,1.2,1.5,2.0,2.5.",
    )
    parser.add_argument(
        "--optimize-loop-presets",
        action="store_true",
        help="Run each LOOP distance separately and rank every coin by its best preset.",
    )
    parser.add_argument(
        "--skip-market-validation",
        action="store_true",
        help="Use cached candles without reloading exchange markets; useful for fast local tuning.",
    )
    args = parser.parse_args()
    loop_distances = _parse_float_list(args.loop_distances) if args.loop_distances else None
    symbols = [symbol.strip().upper() for symbol in args.symbols.split(",") if symbol.strip()]

    if args.optimize_loop_presets:
        config = load_public_config()
        optimizer_distances = loop_distances or [
            float(distance) for distance in config.get("optimizer", {}).get("loop_distances_pct", [0.8, 1.0, 1.2, 1.5, 2.0, 2.5])
        ]
        summary, results = run_loop_optimizer(
            exchange_id=args.exchange,
            history_exchange_id=args.history_exchange,
            days=args.days,
            fee_pct=args.fee_pct,
            preset=args.preset,
            cache_dir=Path(args.cache_dir),
            starting_balance=args.starting_balance,
            trade_size=args.trade_size,
            validate_markets=not args.skip_market_validation,
            all_usdt_pairs=args.all_usdt_pairs,
            max_pairs=args.max_pairs,
            loop_distances=optimizer_distances,
            timeframe=args.timeframe,
            order_count=args.order_count,
            symbols=symbols or None,
        )
        print("OPTIMIZER_SUMMARY")
        for key, value in summary.items():
            print(f"{key}={value}")
        print("RANKED_RESULTS")
        for row in results:
            if row["status"] != "ok":
                print(f"{row['symbol']}|{row['status']}")
                continue
            print(
                "|".join(
                    str(row.get(key, ""))
                    for key in [
                        "symbol",
                        "best_distance_pct",
                        "trades",
                        "wins",
                        "losses",
                        "win_rate_pct",
                        "net_return_pct",
                        "monthly_profit_estimate",
                        "monthly_return_on_trade_size_pct",
                        "avg_hold_hours",
                    ]
                )
            )
        return

    summary, results = run_backtest(
        exchange_id=args.exchange,
        days=args.days,
        fee_pct=args.fee_pct,
        preset=args.preset,
        cache_dir=Path(args.cache_dir),
        starting_balance=args.starting_balance,
        trade_size=args.trade_size,
        history_exchange_id=args.history_exchange,
        validate_markets=not args.skip_market_validation,
        all_usdt_pairs=args.all_usdt_pairs,
        max_pairs=args.max_pairs,
        loop_distances=loop_distances,
        timeframe=args.timeframe,
        order_count=args.order_count,
        symbols=symbols or None,
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
                    "profit_win_rate_pct",
                    "price_net_return_pct",
                    "grid_cycles",
                    "grid_net_return_pct",
                    "gross_return_pct",
                    "net_return_pct",
                    "monthly_profit_estimate",
                    "monthly_return_on_trade_size_pct",
                    "avg_hold_hours",
                ]
            )
        )


if __name__ == "__main__":
    main()
