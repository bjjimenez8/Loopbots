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
        self.preset_name = loop_settings.get("preset_name", "Mid-term")
        self.order_distance_pct = float(loop_settings.get("order_distance_pct", 1.5))
        self.order_count = int(loop_settings.get("order_count", 10))
        self.assumed_round_trip_fee_pct = float(loop_settings.get("assumed_round_trip_fee_pct", 0.2))
        self.take_profit_mode = str(loop_settings.get("take_profit_mode", "price")).lower()
        self.normal_take_profit_min_pct = float(loop_settings.get("normal_take_profit_min_pct", 5.0))
        self.normal_take_profit_max_pct = float(loop_settings.get("normal_take_profit_max_pct", 10.0))
        self.momentum_take_profit_min_pct = float(loop_settings.get("momentum_take_profit_min_pct", 15.0))
        self.momentum_take_profit_max_pct = float(loop_settings.get("momentum_take_profit_max_pct", 20.0))
        self.monitored_stop_loss_min_pct = float(loop_settings.get("monitored_stop_loss_min_pct", 5.0))
        self.monitored_stop_loss_max_pct = float(loop_settings.get("monitored_stop_loss_max_pct", 6.0))
        self.momentum_min_volume_ratio = float(loop_settings.get("momentum_min_volume_ratio", 1.25))
        self.momentum_min_rsi = float(loop_settings.get("momentum_min_rsi", 52.0))
        self.momentum_max_rsi = float(loop_settings.get("momentum_max_rsi", 68.0))

    def analyze_entry(self, symbol: str, candles: pd.DataFrame) -> Signal:
        if self.config.get("entry_style") == "sideways_accumulation":
            return self._analyze_sideways_accumulation(symbol, candles)

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
        breakdown_ok = self._breakdown_ok(df)
        strong_momentum = self._strong_momentum(latest, previous, trend_ok, price_reclaimed_fast_ema)
        loop_plan = self._build_loop_plan(range_window, price, atr, strong_momentum=strong_momentum)
        loop_ready = bool(loop_plan) and self._loop_ready(loop_plan, price, range_position, profile)
        setup_score = self._setup_score(
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
        min_signal_score = float(self.config.get("min_signal_score", 0.0))

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
                    **self.loop_settings,
                    "loop_plan": loop_plan,
                },
            )

        return Signal("HOLD", symbol=symbol, price=price, reason="no entry setup")

    def _analyze_sideways_accumulation(self, symbol: str, candles: pd.DataFrame) -> Signal:
        df = self._with_indicators(candles)
        if len(df) < self._minimum_candles:
            return Signal("HOLD", symbol, price=float(candles["close"].iloc[-1]), reason="not enough data")

        latest = df.iloc[-1]
        previous = df.iloc[-2]
        range_window = df.iloc[-self._range_lookback :]
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
            float(self.config.get("sideways_min_range_width_pct", 5.0))
            <= range_width_pct
            <= float(self.config.get("sideways_max_range_width_pct", 22.0))
        )
        slope_ok = ema_slope_pct <= float(self.config.get("sideways_max_ema_slope_pct", 4.0))
        position_ok = (
            float(self.config.get("sideways_min_range_position", 0.15))
            <= range_position
            <= float(self.config.get("sideways_max_range_position", 0.62))
        )
        touches_ok = support_touches >= int(self.config.get("sideways_min_support_touches", 2)) and resistance_touches >= int(
            self.config.get("sideways_min_resistance_touches", 1)
        )
        bounce_ok = latest["close"] >= latest["low"] * (1 + self.config["bounce_confirmation_pct"]) and latest["close"] >= previous["close"] * 0.995
        rsi_ok = float(self.config.get("sideways_min_rsi", 35)) <= latest["rsi"] <= float(self.config.get("sideways_max_rsi", 62))
        volume_ok = latest["volume_ratio"] >= float(self.config.get("sideways_min_volume_ratio", 0.7))
        momentum_lookback = int(self.config.get("sideways_momentum_lookback", 96))
        local_momentum_ok = (
            len(df) > momentum_lookback
            and latest["close"] > latest["ema_trend"]
            and latest["close"] > df["close"].iloc[-momentum_lookback - 1]
        )
        breakdown_ok = self._breakdown_ok(df)
        loop_plan = self._build_loop_plan(range_window, price, atr, strong_momentum=False)
        profile = self._symbol_profile(symbol)
        loop_ready = bool(loop_plan) and self._loop_ready(loop_plan, price, range_position, profile)
        setup_score = self._sideways_setup_score(
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
        min_signal_score = float(self.config.get("min_signal_score", 0.0))

        if (
            range_ok
            and slope_ok
            and position_ok
            and touches_ok
            and bounce_ok
            and rsi_ok
            and volume_ok
            and local_momentum_ok
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
                    **self.loop_settings,
                    "loop_plan": loop_plan,
                },
            )

        return Signal("HOLD", symbol=symbol, price=price, reason="no sideways setup")

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
        minimum = max(
            self.config["ema_trend"],
            self.config["rsi_period"],
            self.config["atr_period"],
            self.config["volume_sma_period"],
        ) + self.config["pullback_lookback"] + 6
        if self.config.get("entry_style") == "sideways_accumulation":
            minimum = max(
                minimum,
                self.config["ema_trend"] + int(self.config.get("sideways_momentum_lookback", 96)) + 6,
            )
        return minimum

    @property
    def _range_lookback(self) -> int:
        configured = int(self.config.get("range_lookback_bars", 0) or 0)
        return max(configured, self.config["pullback_lookback"] * 3, 12)

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

    def _build_loop_plan(
        self,
        range_window: pd.DataFrame,
        price: float,
        atr: float,
        strong_momentum: bool = False,
    ) -> dict | None:
        range_low = float(range_window["low"].min())
        range_high = float(range_window["high"].max())

        if range_high <= range_low or price <= 0 or atr <= 0:
            return None

        raw_range_width_pct = ((range_high - range_low) / price) * 100
        order_count = self.order_count

        if self.order_distance_pct < 0.5 or order_count < 10 or order_count > 40 or order_count % 2 != 0:
            return None

        precision = self._price_precision(price)
        half_order_count = order_count // 2
        atr_pct = (atr / price) * 100
        if strong_momentum:
            take_profit_pct = min(
                max(self.momentum_take_profit_min_pct, atr_pct * 4.0),
                self.momentum_take_profit_max_pct,
            )
            target_tier = f"Momentum {self.momentum_take_profit_min_pct:g}-{self.momentum_take_profit_max_pct:g}%"
        else:
            take_profit_pct = min(
                max(self.normal_take_profit_min_pct, atr_pct * 2.5),
                self.normal_take_profit_max_pct,
            )
            target_tier = (
                f"Standard {self.normal_take_profit_min_pct:g}%"
                if self.normal_take_profit_min_pct == self.normal_take_profit_max_pct
                else f"Normal {self.normal_take_profit_min_pct:g}-{self.normal_take_profit_max_pct:g}%"
            )

        # The recommended Bitsgap range must be wide enough to contain the target.
        order_distance_pct = max(self.order_distance_pct, take_profit_pct / half_order_count)
        order_distance_pct = round(order_distance_pct, 2)
        half_range_pct = order_distance_pct * half_order_count
        estimated_low_price = round(price * (1 - (half_range_pct / 100)), precision)
        estimated_high_price = round(price * (1 + (half_range_pct / 100)), precision)
        if estimated_low_price <= 0 or estimated_high_price <= price:
            return None

        safety_exit_pct = min(
            max(self.monitored_stop_loss_min_pct, atr_pct * 1.5),
            self.monitored_stop_loss_max_pct,
        )
        safety_exit_price = round(price * (1 - (safety_exit_pct / 100)), precision)
        take_profit_price = round(price * (1 + (take_profit_pct / 100)), precision)
        reward_to_risk = (take_profit_price - price) / (price - safety_exit_price) if price > safety_exit_price else 0.0

        if take_profit_price > estimated_high_price:
            return None

        return {
            "preset_name": self.preset_name,
            "take_profit_mode": self.take_profit_mode,
            "target_tier": target_tier,
            "strong_momentum": strong_momentum,
            "take_profit_pct": round(take_profit_pct, 2),
            "monitored_stop_loss_pct": round(safety_exit_pct, 2),
            "native_stop_loss_supported": False,
            "safety_exit_price": safety_exit_price,
            "take_profit_price": take_profit_price,
            "order_distance_pct": order_distance_pct,
            "order_count": order_count,
            "estimated_low_price": estimated_low_price,
            "estimated_high_price": estimated_high_price,
            "range_width_pct": round(raw_range_width_pct, 2),
            "range_low": round(range_low, precision),
            "range_high": round(range_high, precision),
            "bitsgap_range_pct": round(half_range_pct * 2, 2),
            "fee_buffer_pct": round(order_distance_pct - self.assumed_round_trip_fee_pct, 2),
            "reward_to_risk": round(reward_to_risk, 2),
        }

    def _strong_momentum(
        self,
        latest: pd.Series,
        previous: pd.Series,
        trend_ok: bool,
        price_reclaimed_fast_ema: bool,
    ) -> bool:
        return bool(
            trend_ok
            and price_reclaimed_fast_ema
            and latest["close"] > previous["close"]
            and float(latest["volume_ratio"]) >= self.momentum_min_volume_ratio
            and self.momentum_min_rsi <= float(latest["rsi"]) <= self.momentum_max_rsi
        )

    @staticmethod
    def _setup_score(
        latest: pd.Series,
        trend_ok: bool,
        price_reclaimed_fast_ema: bool,
        pullback_ok: bool,
        bounce_ok: bool,
        rsi_ok: bool,
        volume_ok: bool,
        loop_plan: dict | None,
        range_position: float,
    ) -> int:
        score = 0
        score += 20 if trend_ok else 0
        score += 10 if price_reclaimed_fast_ema else 0
        score += 15 if pullback_ok else 0
        score += 15 if bounce_ok else 0
        score += 10 if rsi_ok else 0
        score += 10 if volume_ok else 0

        if loop_plan:
            reward_to_risk = float(loop_plan.get("reward_to_risk", 0.0))
            fee_buffer_pct = float(loop_plan.get("fee_buffer_pct", 0.0))
            score += min(max(int(reward_to_risk * 8), 0), 10)
            score += min(max(int(fee_buffer_pct * 5), 0), 5)

        if range_position <= 0.45:
            score += 5
        elif range_position <= 0.6:
            score += 3

        rsi = float(latest.get("rsi", 0.0))
        if 48 <= rsi <= 62:
            score += 5

        return min(score, 100)

    @staticmethod
    def _sideways_setup_score(
        range_ok: bool,
        slope_ok: bool,
        position_ok: bool,
        touches_ok: bool,
        bounce_ok: bool,
        rsi_ok: bool,
        volume_ok: bool,
        breakdown_ok: bool,
        loop_plan: dict | None,
        range_position: float,
        range_width_pct: float,
    ) -> int:
        score = 0
        score += 20 if range_ok else 0
        score += 15 if slope_ok else 0
        score += 15 if position_ok else 0
        score += 10 if touches_ok else 0
        score += 10 if bounce_ok else 0
        score += 10 if rsi_ok else 0
        score += 8 if volume_ok else 0
        score += 7 if breakdown_ok else 0

        if loop_plan:
            reward_to_risk = float(loop_plan.get("reward_to_risk", 0.0))
            score += min(max(int(reward_to_risk * 5), 0), 5)

        if 8 <= range_width_pct <= 18:
            score += 5
        if 0.25 <= range_position <= 0.5:
            score += 5

        return min(score, 100)

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

    def _breakdown_ok(self, df: pd.DataFrame) -> bool:
        lookback = int(self.config.get("recent_drop_lookback", 24))
        max_drop_pct = float(self.config.get("max_recent_drop_pct", 8.0))
        if lookback <= 0 or len(df) <= lookback:
            return True

        previous_close = float(df["close"].iloc[-lookback])
        latest_close = float(df["close"].iloc[-1])
        if previous_close <= 0:
            return True

        recent_change_pct = ((latest_close / previous_close) - 1) * 100
        return recent_change_pct >= -max_drop_pct

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
                "max_range_position": 0.6,
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
            "max_range_position": 0.6,
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
