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
        self.preset_name = loop_settings.get("preset_name", "Mid-term")
        self.order_distance_pct = float(loop_settings.get("order_distance_pct", 1.5))
        self.order_count = int(loop_settings.get("order_count", 10))
        self.assumed_round_trip_fee_pct = float(loop_settings.get("assumed_round_trip_fee_pct", 0.2))

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
        loop_ready = bool(loop_plan) and self._loop_ready(loop_plan, price, range_position, profile)

        if trend_ok and price_reclaimed_fast_ema and pullback_ok and bounce_ok and rsi_ok and volume_ok and loop_ready:
            assert loop_plan is not None
            return Signal(
                "ENTER",
                symbol=symbol,
                price=price,
                take_profit_price=loop_plan["take_profit_price"],
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
        order_distance_pct = self.order_distance_pct
        order_count = self.order_count

        if order_distance_pct < 0.5 or order_count < 10 or order_count % 2 != 0:
            return None

        precision = self._price_precision(price)
        safety_exit_pct = max(order_distance_pct, (atr / price) * 100 * 0.85)
        take_profit_pct = max(order_distance_pct * 1.15, (atr / price) * 100 * 1.05)
        safety_exit_price = round(price * (1 - (safety_exit_pct / 100)), precision)
        take_profit_price = round(price * (1 + (take_profit_pct / 100)), precision)
        reward_to_risk = (take_profit_price - price) / (price - safety_exit_price) if price > safety_exit_price else 0.0

        return {
            "preset_name": self.preset_name,
            "safety_exit_price": safety_exit_price,
            "take_profit_price": take_profit_price,
            "order_distance_pct": order_distance_pct,
            "order_count": order_count,
            "range_width_pct": round(raw_range_width_pct, 2),
            "range_low": round(range_low, precision),
            "range_high": round(range_high, precision),
            "fee_buffer_pct": round(order_distance_pct - self.assumed_round_trip_fee_pct, 2),
            "reward_to_risk": round(reward_to_risk, 2),
        }

    def _loop_ready(self, loop_plan: dict, price: float, range_position: float, profile: dict) -> bool:
        safety_exit_price = float(loop_plan["safety_exit_price"])
        order_distance_pct = float(loop_plan["order_distance_pct"])
        order_count = int(loop_plan["order_count"])
        range_width_pct = float(loop_plan["range_width_pct"])
        reward_to_risk = float(loop_plan["reward_to_risk"])

        if order_count < 10 or order_count > 40 or order_count % 2 != 0:
            return False
        if order_distance_pct < 0.5:
            return False
        if range_width_pct < max(order_distance_pct * profile["min_range_multiple"], profile["min_range_floor_pct"]):
            return False
        if range_position > profile["max_range_position"]:
            return False
        if (order_distance_pct - self.assumed_round_trip_fee_pct) < profile["fee_edge_buffer"]:
            return False
        if reward_to_risk < profile["min_reward_to_risk"]:
            return False

        return safety_exit_price < price

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
                "fee_edge_buffer": 0.08,
                "min_reward_to_risk": 0.9,
                "max_range_position": 0.76,
                "min_range_multiple": 1.5,
                "min_range_floor_pct": 1.2,
            }

        return {
            "ema_reclaim_buffer": 1.0,
            "bounce_multiplier": 1.0,
            "previous_close_buffer": 1.0,
            "open_buffer": 0.999,
            "rsi_low_buffer": 0,
            "rsi_high_buffer": 0,
            "volume_buffer": 0.0,
            "fee_edge_buffer": 0.1,
            "min_reward_to_risk": 0.95,
            "max_range_position": 0.7,
            "min_range_multiple": 1.7,
            "min_range_floor_pct": 1.5,
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
