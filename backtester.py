from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from strategy import LoopStrategy


@dataclass(frozen=True)
class BacktestResult:
    symbol: str
    trades: int
    wins: int
    losses: int
    net_return_pct: float


class Backtester:
    def __init__(self, strategy: LoopStrategy) -> None:
        self.strategy = strategy

    def run_csv(self, symbol: str, csv_path: str) -> BacktestResult:
        candles = pd.read_csv(Path(csv_path))
        candles["timestamp"] = pd.to_datetime(candles["timestamp"], utc=True)
        active_trade: dict | None = None
        wins = 0
        losses = 0
        net_return_pct = 0.0

        for index in range(self.strategy._minimum_candles, len(candles)):
            window = candles.iloc[: index + 1].reset_index(drop=True)

            if active_trade is None:
                signal = self.strategy.analyze_entry(symbol, window)
                if signal.signal_type == "ENTER":
                    active_trade = {
                        "entry_price": signal.price,
                        "take_profit_price": signal.take_profit_price,
                        "safety_exit_price": signal.safety_exit_price,
                    }
                continue

            price = float(window["close"].iloc[-1])
            if price >= float(active_trade["take_profit_price"]):
                wins += 1
                net_return_pct += ((price / active_trade["entry_price"]) - 1) * 100
                active_trade = None
            elif price <= float(active_trade["safety_exit_price"]):
                losses += 1
                net_return_pct += ((price / active_trade["entry_price"]) - 1) * 100
                active_trade = None

        return BacktestResult(
            symbol=symbol,
            trades=wins + losses,
            wins=wins,
            losses=losses,
            net_return_pct=round(net_return_pct, 2),
        )
