from __future__ import annotations

import math
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
        self.min_order_distance_pct = 0.5
        self.max_order_count = 40
        self.min_order_count = 4
        self.assumed_round_trip_fee_pct = 0.2

    def analyze_entry(self, symbol: str, candles: pd.DataFrame) -> Signal:
        profile = self._symbol_profile(symbol)
        df = self._with_indicators(candles)
        if len(df) < self._minimum_candles:
            return Signal("HOLD", symbol, price=float(candles["close"].iloc[-1]), reason="not enough data")

        latest = df.iloc[-1]
        previous = df.iloc[-2]
        recent = df.iloc[-self.config["pullback_lookback"] :]
        range_window = df.iloc[-self._range_lookback :]
        price = float(latest["close"])
        atr = float(latest["atr"])

        trend_ok = (
            latest["ema_fast"] > latest["ema_slow"] > latest["ema_trend"]
            and latest["ema_trend"] > previous["ema_trend"]
        )
        price_reclaimed_fast_ema = latest["close"] > latest["ema_fast"] * profile["ema_reclaim_buffer"]
        recent_high = float(recent["high"].max())
        pullback_pct = (recent_high - price) / recent_high if recent_high else 0.0
        pullback_ok = self.config["pullback_min_pct"] <= pullback_pct <= self.config["pullback_max_pct"]
        bounce_ok = (
            latest["close"] >= latest["low"] * (1 + (self.config["bounce_confirmation_pct"] * profile["bounce_multiplier"]))
            and latest["close"] >= previous["close"] * profile["previous_close_buffer"]
            and latest["close"] >= latest["open"] * profile["open_buffer"]
        )
        rsi_ok = (
            (self.config["min_rsi"] - profile["rsi_low_buffer"])
            <= latest["rsi"]
            <= (self.config["max_rsi"] + profile["rsi_high_buffer"])
        )
        volume_ok = latest["volume_ratio"] >= max(self.config["min_volume_ratio"] - profile["volume_buffer"], 0.6)
        loop_plan = self._build_loop_plan(range_window, price, atr)
        loop_ready = bool(loop_plan) and self._loop_ready(loop_plan, price, profile)

        if trend_ok and price_reclaimed_fast_ema and pullback_ok and bounce_ok and rsi_ok and volume_ok and loop_ready:
            assert loop_plan is not None
            return Signal(
                "ENTER",
                symbol=symbol,
                price=price,
                take_profit_price=loop_plan["high_price"],
                safety_exit_price=loop_plan["safety_exit_price"],
                reason="Trend up, shallow pullback, bounce confirmed",
                loop_settings={
                    **self.loop_settings,
                    "loop_plan": loop_plan,
                },
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
        ) + self.config["pullback_lookback"] + 6

    @property
    def _range_lookback(self) -> int:
        return max(self.config["pullback_lookback"] * 3, 12)

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

    def _build_loop_plan(self, range_window: pd.DataFrame, price: float, atr: float) -> dict | None:
        range_low = float(range_window["low"].min())
        range_high = float(range_window["high"].max())

        if range_high <= range_low or price <= 0 or atr <= 0:
            return None

        raw_range_width_pct = ((range_high - range_low) / price) * 100
        order_distance_pct = max(
            self.min_order_distance_pct,
            round(max((atr / price) * 100 * 0.65, self.assumed_round_trip_fee_pct * 2.0), 2),
        )
        min_required_order_count = max(
            self.min_order_count,
            math.ceil(raw_range_width_pct / order_distance_pct) + 2,
        )
        if min_required_order_count > self.max_order_count:
            return None

        order_count = min_required_order_count
        precision = self._price_precision(price)
        safety_exit_price = round(range_low - (atr * 0.15), precision)
        high_price = round(range_high, precision)
        low_price = round(range_low, precision)
        reward_to_risk = (high_price - price) / (price - safety_exit_price) if price > safety_exit_price else 0.0

        return {
            "low_price": low_price,
            "high_price": high_price,
            "safety_exit_price": safety_exit_price,
            "order_distance_pct": order_distance_pct,
            "order_count": order_count,
            "min_required_order_count": min_required_order_count,
            "range_width_pct": round(raw_range_width_pct, 2),
            "fee_buffer_pct": round(order_distance_pct - self.assumed_round_trip_fee_pct, 2),
            "reward_to_risk": round(reward_to_risk, 2),
        }

    def _loop_ready(self, loop_plan: dict, price: float, profile: dict) -> bool:
        low_price = float(loop_plan["low_price"])
        high_price = float(loop_plan["high_price"])
        safety_exit_price = float(loop_plan["safety_exit_price"])
        order_distance_pct = float(loop_plan["order_distance_pct"])
        order_count = int(loop_plan["order_count"])
        min_required_order_count = int(loop_plan["min_required_order_count"])
        range_width_pct = float(loop_plan["range_width_pct"])
        reward_to_risk = float(loop_plan["reward_to_risk"])

        if not (self.min_order_count <= order_count <= self.max_order_count):
            return False
        if order_count < min_required_order_count:
            return False
        if order_distance_pct < self.min_order_distance_pct:
            return False
        if range_width_pct < order_distance_pct * max(order_count - 2, 1):
            return False
        if (order_distance_pct - self.assumed_round_trip_fee_pct) < profile["fee_edge_buffer"]:
            return False
        if reward_to_risk < profile["min_reward_to_risk"]:
            return False
        if not (low_price < price < high_price):
            return False

        range_position = (price - low_price) / (high_price - low_price)
        if range_position > profile["max_range_position"]:
            return False

        return safety_exit_price < low_price

    @staticmethod
    def _symbol_profile(symbol: str) -> dict:
        if symbol in {"BTC/USDT", "ETH/USDT"}:
            return {
                "ema_reclaim_buffer": 0.998,
                "bounce_multiplier": 0.85,
                "previous_close_buffer": 0.999,
                "open_buffer": 0.998,
                "rsi_low_buffer": 2,
                "rsi_high_buffer": 2,
                "volume_buffer": 0.1,
                "fee_edge_buffer": 0.12,
                "min_reward_to_risk": 0.7,
                "max_range_position": 0.8,
            }

        return {
            "ema_reclaim_buffer": 1.0,
            "bounce_multiplier": 1.0,
            "previous_close_buffer": 1.0,
            "open_buffer": 0.999,
            "rsi_low_buffer": 0,
            "rsi_high_buffer": 0,
            "volume_buffer": 0.0,
            "fee_edge_buffer": 0.15,
            "min_reward_to_risk": 0.8,
            "max_range_position": 0.75,
        }

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
