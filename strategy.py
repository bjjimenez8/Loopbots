from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd


SignalType = Literal["ENTER", "EXIT", "HOLD"]


@dataclass(frozen=True)
class Signal:
    signal_type: SignalType
    symbol: str
    price: float
    take_profit_price: float | None = None
    safety_exit_price: float | None = None
    reason: str = ""
    loop_settings: dict | None = None


class LoopStrategy:
    def __init__(self, strategy_config: dict, loop_settings: dict) -> None:
        self.config = strategy_config
        self.loop_settings = loop_settings

    def analyze_entry(self, symbol: str, candles: pd.DataFrame) -> Signal:
        df = self._with_indicators(candles)
        if len(df) < self._minimum_candles:
            return Signal("HOLD", symbol, price=float(candles["close"].iloc[-1]), reason="not enough data")

        latest = df.iloc[-1]
        recent = df.iloc[-self.config["pullback_lookback"] :]
        price = float(latest["close"])
        atr = float(latest["atr"])

        trend_ok = latest["ema_fast"] > latest["ema_slow"] > latest["ema_trend"]
        price_reclaimed_fast_ema = latest["close"] > latest["ema_fast"]
        recent_high = float(recent["high"].max())
        pullback_pct = (recent_high - price) / recent_high if recent_high else 0.0
        pullback_ok = self.config["pullback_min_pct"] <= pullback_pct <= self.config["pullback_max_pct"]
        bounce_ok = latest["close"] >= latest["low"] * (1 + self.config["bounce_confirmation_pct"])
        rsi_ok = self.config["min_rsi"] <= latest["rsi"] <= self.config["max_rsi"]
        volume_ok = latest["volume_ratio"] >= self.config["min_volume_ratio"]

        if trend_ok and price_reclaimed_fast_ema and pullback_ok and bounce_ok and rsi_ok and volume_ok:
            take_profit = price + (atr * self.config["take_profit_atr_multiple"])
            safety_exit = price - (atr * self.config["safety_exit_atr_multiple"])
            return Signal(
                "ENTER",
                symbol=symbol,
                price=price,
                take_profit_price=round(take_profit, self._price_precision(price)),
                safety_exit_price=round(safety_exit, self._price_precision(price)),
                reason="short-term uptrend pullback bounce",
                loop_settings=self.loop_settings,
            )

        return Signal("HOLD", symbol=symbol, price=price, reason="no entry setup")

    def analyze_exit(self, symbol: str, candles: pd.DataFrame, active_trade: dict) -> Signal:
        price = float(candles["close"].iloc[-1])
        safety_exit_price = float(active_trade["safety_exit_price"])

        if price <= safety_exit_price:
            return Signal(
                "EXIT",
                symbol=symbol,
                price=price,
                safety_exit_price=safety_exit_price,
                reason="safety exit triggered",
            )

        return Signal("HOLD", symbol=symbol, price=price, reason="trade still active")

    @property
    def _minimum_candles(self) -> int:
        return max(
            self.config["ema_trend"],
            self.config["rsi_period"],
            self.config["atr_period"],
            self.config["volume_sma_period"],
        ) + 5

    def _with_indicators(self, candles: pd.DataFrame) -> pd.DataFrame:
        df = candles.copy()
        df["ema_fast"] = df["close"].ewm(span=self.config["ema_fast"], adjust=False).mean()
        df["ema_slow"] = df["close"].ewm(span=self.config["ema_slow"], adjust=False).mean()
        df["ema_trend"] = df["close"].ewm(span=self.config["ema_trend"], adjust=False).mean()
        df["rsi"] = self._rsi(df["close"], self.config["rsi_period"])
        df["atr"] = self._atr(df, self.config["atr_period"])
        df["volume_sma"] = df["volume"].rolling(self.config["volume_sma_period"]).mean()
        df["volume_ratio"] = df["volume"] / df["volume_sma"]
        return df.dropna().reset_index(drop=True)

    @staticmethod
    def _rsi(close: pd.Series, period: int) -> pd.Series:
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(period).mean()
        loss = (-delta.clip(upper=0)).rolling(period).mean()
        rs = gain / loss.where(loss != 0)
        return 100 - (100 / (1 + rs))

    @staticmethod
    def _atr(df: pd.DataFrame, period: int) -> pd.Series:
        prev_close = df["close"].shift(1)
        true_range = pd.concat(
            [
                df["high"] - df["low"],
                (df["high"] - prev_close).abs(),
                (df["low"] - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        return true_range.rolling(period).mean()

    @staticmethod
    def _price_precision(price: float) -> int:
        if price >= 1000:
            return 2
        if price >= 1:
            return 4
        return 6
