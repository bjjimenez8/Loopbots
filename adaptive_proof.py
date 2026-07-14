from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from run_backtest import _combined_trade_returns, _new_grid_state, _update_grid_state
from strategy import Signal


REGISTRY_VERSION = "adaptive-proof-v1"


@dataclass(frozen=True)
class LoopCandidate:
    name: str
    timeframe: str
    hold_days: int
    lookback_days: int
    order_distance_pct: float
    order_count: int
    take_profit_pct: float
    stop_loss_pct: float
    regime: str


LOOP_CANDIDATES = [
    LoopCandidate("short_range", "1h", 7, 21, 1.0, 10, 3.0, 5.0, "sideways_bull"),
    LoopCandidate("mid_accumulation", "1h", 14, 30, 1.2, 10, 5.0, 7.0, "sideways_bull"),
    LoopCandidate("compound_range", "1h", 21, 30, 1.0, 20, 5.0, 8.0, "bull_or_sideways"),
    LoopCandidate("long_accumulation", "1h", 30, 45, 1.5, 20, 8.0, 10.0, "bull"),
    LoopCandidate("momentum_12", "1h", 21, 30, 2.0, 20, 12.0, 8.0, "bull"),
    LoopCandidate("momentum_20", "1h", 30, 45, 2.5, 20, 20.0, 10.0, "bull"),
]


def build_registry(cache_dir: Path, output_path: Path, fee_loop_pct: float) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for path in sorted(cache_dir.glob("okx_*_USDT_1h_730d.csv")):
        symbol = _symbol_from_cache(path)
        candles = _load_candles(path)
        if len(candles) < 17_000:
            continue
        split_index = len(candles) // 2
        train = candles.iloc[:split_index].reset_index(drop=True)
        test = candles.iloc[split_index:].reset_index(drop=True)
        rows.append(_select_loop_profile(symbol, train, test, fee_loop_pct))

    registry = {
        "version": REGISTRY_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "history_source": "OKX public candles for Kraken-listed spot symbols",
        "method": "first-half selection, untouched second-half validation, non-overlapping starts",
        "fees": {"loop_round_trip_pct": fee_loop_pct},
        "profiles": rows,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(registry, indent=2), encoding="utf-8")
    return registry


def _select_loop_profile(symbol: str, train: pd.DataFrame, test: pd.DataFrame, fee_pct: float) -> dict[str, Any]:
    train_results = [
        (candidate, _rolling_loop(train, candidate, fee_pct))
        for candidate in LOOP_CANDIDATES
    ]
    candidate, train_stats = max(train_results, key=lambda item: _selection_score(item[1]))
    test_stats = _rolling_loop(test, candidate, fee_pct)
    proven, reasons = _proof_decision(train_stats, test_stats, max_drawdown_pct=12.0)
    return _profile_row("LOOP", symbol, candidate, train_stats, test_stats, proven, reasons, fee_pct)


def _rolling_loop(candles: pd.DataFrame, candidate: LoopCandidate, fee_pct: float) -> dict[str, Any]:
    prepared = _with_regime_indicators(candles, candidate.lookback_days)
    bars_per_day = 24
    hold_bars = candidate.hold_days * bars_per_day
    lookback_bars = candidate.lookback_days * bars_per_day
    returns: list[float] = []
    drawdowns: list[float] = []
    cycles: list[int] = []
    start_index = lookback_bars
    while start_index + hold_bars < len(prepared):
        if _loop_launch_ok(prepared.iloc[start_index], candidate.regime):
            result = _simulate_loop_window(
                prepared.iloc[start_index : start_index + hold_bars + 1].reset_index(drop=True),
                candidate,
                fee_pct,
            )
            returns.append(result["return_pct"])
            drawdowns.append(result["max_drawdown_pct"])
            cycles.append(result["cycles"])
            start_index += hold_bars
        else:
            start_index += bars_per_day
    return _summarize_returns(returns, drawdowns, cycles, candidate.hold_days)


def _simulate_loop_window(candles: pd.DataFrame, candidate: LoopCandidate, fee_pct: float) -> dict[str, Any]:
    entry_price = float(candles["close"].iloc[0])
    signal = Signal(
        "ENTER",
        symbol="BACKTEST",
        price=entry_price,
        loop_settings={
            "order_distance_pct": candidate.order_distance_pct,
            "order_count": candidate.order_count,
            "loop_plan": {
                "order_distance_pct": candidate.order_distance_pct,
                "order_count": candidate.order_count,
            },
        },
    )
    active = {
        "entry_price": entry_price,
        "grid": _new_grid_state(signal, fee_pct),
    }
    minimum_return = 0.0
    final_return = -fee_pct
    for _, candle in candles.iloc[1:].iterrows():
        _update_grid_state(active["grid"], candle)
        current_price = float(candle["close"])
        total_return = _combined_trade_returns(active, current_price, fee_pct)["net_return_pct"]
        minimum_return = min(minimum_return, total_return)
        final_return = total_return
        if total_return >= candidate.take_profit_pct or total_return <= -candidate.stop_loss_pct:
            break
    return {
        "return_pct": round(final_return, 4),
        "max_drawdown_pct": round(minimum_return, 4),
        "cycles": int(active["grid"]["cycles"]),
    }


def _with_regime_indicators(candles: pd.DataFrame, lookback_days: int) -> pd.DataFrame:
    frame = candles.copy()
    close = frame["close"].astype(float)
    bars = max(lookback_days * 24, 24)
    frame["ema50"] = close.ewm(span=50, adjust=False).mean()
    frame["ema200"] = close.ewm(span=200, adjust=False).mean()
    frame["ema50_prior"] = frame["ema50"].shift(24)
    frame["lookback_return_pct"] = close.pct_change(bars) * 100
    frame["range_high"] = frame["high"].rolling(bars).max()
    frame["range_low"] = frame["low"].rolling(bars).min()
    span = frame["range_high"] - frame["range_low"]
    frame["range_width_pct"] = span / close * 100
    frame["range_position"] = (close - frame["range_low"]) / span
    return frame


def _loop_launch_ok(row: pd.Series, regime: str) -> bool:
    required = ["ema50", "ema200", "ema50_prior", "lookback_return_pct", "range_width_pct", "range_position"]
    if any(pd.isna(row.get(key)) for key in required):
        return False
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


def _summarize_returns(returns: list[float], drawdowns: list[float], cycles: list[int], hold_days: int) -> dict[str, Any]:
    if not returns:
        return {
            "starts": 0,
            "win_rate_pct": 0.0,
            "avg_return_pct": 0.0,
            "avg_win_pct": 0.0,
            "avg_loss_pct": 0.0,
            "profit_factor": 0.0,
            "worst_return_pct": 0.0,
            "worst_drawdown_pct": 0.0,
            "avg_cycles": 0.0,
            "monthly_pct": 0.0,
        }
    wins = [value for value in returns if value > 0]
    losses = [value for value in returns if value <= 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    average = sum(returns) / len(returns)
    return {
        "starts": len(returns),
        "win_rate_pct": round(len(wins) / len(returns) * 100, 2),
        "avg_return_pct": round(average, 2),
        "avg_win_pct": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "avg_loss_pct": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else (99.0 if gross_profit else 0.0),
        "worst_return_pct": round(min(returns), 2),
        "worst_drawdown_pct": round(min(drawdowns), 2) if drawdowns else 0.0,
        "avg_cycles": round(sum(cycles) / len(cycles), 2) if cycles else 0.0,
        "monthly_pct": round(average * (30 / hold_days), 2),
    }


def _selection_score(stats: dict[str, Any]) -> tuple[float, float, float, int]:
    starts = int(stats.get("starts", 0))
    eligible = 1.0 if starts >= 8 and float(stats.get("avg_return_pct", 0.0)) > 0 else 0.0
    return (
        eligible,
        float(stats.get("monthly_pct", 0.0)),
        float(stats.get("profit_factor", 0.0)),
        starts,
    )


def _proof_decision(train: dict[str, Any], test: dict[str, Any], max_drawdown_pct: float) -> tuple[bool, list[str]]:
    checks = {
        "at least 8 non-overlapping starts in each half": int(train["starts"]) >= 8 and int(test["starts"]) >= 8,
        "positive average return in both halves": float(train["avg_return_pct"]) > 0 and float(test["avg_return_pct"]) > 0,
        "validation win rate at least 65%": float(test["win_rate_pct"]) >= 65,
        "validation average return at least 0.25%": float(test["avg_return_pct"]) >= 0.25,
        "validation profit factor at least 1.15": float(test["profit_factor"]) >= 1.15,
        "validation worst drawdown within limit": abs(float(test["worst_drawdown_pct"])) <= max_drawdown_pct,
    }
    return all(checks.values()), [label for label, passed in checks.items() if not passed]


def _profile_row(
    bot: str,
    symbol: str,
    candidate: LoopCandidate,
    train: dict[str, Any],
    test: dict[str, Any],
    proven: bool,
    reasons: list[str],
    fee_pct: float,
) -> dict[str, Any]:
    settings = asdict(candidate)
    return {
        "bot": bot,
        "symbol": symbol,
        "status": "Proven" if proven else "Needs stronger proof",
        "proof_model": REGISTRY_VERSION,
        "settings": settings,
        "fee_pct": fee_pct,
        "historical_starts": int(train["starts"]) + int(test["starts"]),
        "historical_non_overlapping": True,
        "train": train,
        "test": test,
        "failed_checks": reasons,
    }


def _symbol_from_cache(path: Path) -> str:
    stem = path.stem.removeprefix("okx_").removesuffix("_1h_730d")
    base, quote = stem.rsplit("_", 1)
    return f"{base}/{quote}"


def _load_candles(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    for column in ["timestamp", "open", "high", "low", "close", "volume"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.dropna().drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the adaptive LOOP proof registry.")
    parser.add_argument("--cache-dir", default="data/backtests")
    parser.add_argument("--output", default="adaptive_proof_registry.json")
    parser.add_argument("--loop-fee-pct", type=float, default=0.5)
    args = parser.parse_args()
    registry = build_registry(Path(args.cache_dir), Path(args.output), args.loop_fee_pct)
    proven = [row for row in registry["profiles"] if row["status"] == "Proven"]
    print(f"profiles={len(registry['profiles'])}")
    print(f"proven={len(proven)}")
    for row in registry["profiles"]:
        settings = row["settings"]
        print(
            f"{row['bot']}|{row['symbol']}|{row['status']}|{settings['name']}|"
            f"train={row['train']['avg_return_pct']}%|test={row['test']['avg_return_pct']}%|"
            f"test_wr={row['test']['win_rate_pct']}%|starts={row['historical_starts']}"
        )


if __name__ == "__main__":
    main()
