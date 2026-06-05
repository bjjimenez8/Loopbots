from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import ccxt
import pandas as pd


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class GridSetup:
    symbol: str
    preset_name: str
    lower_pct: float
    upper_pct: float
    levels: int
    take_profit_pct: float
    stop_loss_pct: float
    historical_win_rate_pct: float
    historical_avg_return_pct: float
    historical_monthly_pct: float
    historical_avg_drawdown_pct: float
    historical_worst_drawdown_pct: float
    historical_alerts_per_month: float
    score: int


@dataclass(frozen=True)
class GridWatchConfig:
    enabled: bool
    exchange_id: str
    timeframe: str
    candle_limit: int
    investment_usdt: float
    filter_lookback_days: float
    cooldown_days: float
    state_file: str
    setups: list[GridSetup]


class GridWatchService:
    def __init__(self, config: GridWatchConfig) -> None:
        self.config = config
        if not hasattr(ccxt, config.exchange_id):
            raise ValueError(f"Unsupported GRID exchange id: {config.exchange_id}")
        exchange_class = getattr(ccxt, config.exchange_id)
        self.exchange = exchange_class({"enableRateLimit": True})
        self._state_path = Path(config.state_file)
        self._state_path.parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_config(cls, raw_config: dict[str, Any], project_root: Path) -> GridWatchService:
        setups = [
            GridSetup(
                symbol=str(item["symbol"]),
                preset_name=str(item["preset_name"]),
                lower_pct=float(item["lower_pct"]),
                upper_pct=float(item["upper_pct"]),
                levels=int(item["levels"]),
                take_profit_pct=float(item.get("take_profit_pct", raw_config.get("take_profit_pct", 5.0))),
                stop_loss_pct=float(item.get("stop_loss_pct", raw_config.get("stop_loss_pct", 5.0))),
                historical_win_rate_pct=float(item["historical_win_rate_pct"]),
                historical_avg_return_pct=float(item["historical_avg_return_pct"]),
                historical_monthly_pct=float(item["historical_monthly_pct"]),
                historical_avg_drawdown_pct=float(item["historical_avg_drawdown_pct"]),
                historical_worst_drawdown_pct=float(item["historical_worst_drawdown_pct"]),
                historical_alerts_per_month=float(item["historical_alerts_per_month"]),
                score=int(item["score"]),
            )
            for item in raw_config.get("setups", [])
        ]
        state_file = str(project_root / raw_config.get("state_file", "data/grid_watch_state.json"))
        return cls(
            GridWatchConfig(
                enabled=bool(raw_config.get("enabled", False)),
                exchange_id=str(raw_config.get("exchange_id", "kraken")),
                timeframe=str(raw_config.get("timeframe", "1h")),
                candle_limit=int(raw_config.get("candle_limit", 360)),
                investment_usdt=float(raw_config.get("investment_usdt", 5000.0)),
                filter_lookback_days=float(raw_config.get("filter_lookback_days", 14.0)),
                cooldown_days=float(raw_config.get("cooldown_days", 14.0)),
                state_file=state_file,
                setups=setups,
            )
        )

    def find_alerts(self) -> list[dict[str, Any]]:
        if not self.config.enabled:
            return []

        state = self._load_state()
        alerts: list[dict[str, Any]] = []
        for setup in self.config.setups:
            key = self._state_key(setup)
            if self._in_cooldown(state.get(key)):
                continue

            try:
                candles = self._fetch_candles(setup.symbol)
            except Exception:
                LOGGER.exception("Failed to fetch GRID candles for %s", setup.symbol)
                continue

            if not self._passes_strict_sideways(candles):
                continue

            alert = self._build_alert(setup, candles)
            alerts.append(alert)
            state[key] = {
                "last_alert_at": datetime.now(UTC).isoformat(),
                "symbol": setup.symbol,
                "preset_name": setup.preset_name,
            }

        if alerts:
            self._save_state(state)
        return alerts

    def _fetch_candles(self, symbol: str) -> pd.DataFrame:
        rows = self.exchange.fetch_ohlcv(symbol, timeframe=self.config.timeframe, limit=self.config.candle_limit)
        if not rows:
            raise RuntimeError(f"No GRID candles returned for {symbol}")

        df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        for column in ["open", "high", "low", "close", "volume"]:
            df[column] = pd.to_numeric(df[column], errors="coerce")
        return df.dropna().reset_index(drop=True)

    def _passes_strict_sideways(self, candles: pd.DataFrame) -> bool:
        if candles.empty:
            return False

        cutoff = candles["timestamp"].iloc[-1] - pd.Timedelta(days=self.config.filter_lookback_days)
        lookback = candles[candles["timestamp"] >= cutoff]
        if len(lookback) < 20:
            return False

        first_close = float(lookback["close"].iloc[0])
        current_close = float(lookback["close"].iloc[-1])
        high_price = float(lookback["high"].max())
        low_price = float(lookback["low"].min())
        if first_close <= 0 or current_close <= 0 or low_price <= 0 or high_price <= low_price:
            return False

        trend_return_pct = ((current_close / first_close) - 1) * 100
        range_pct = ((high_price / low_price) - 1) * 100
        directional_efficiency = abs(trend_return_pct) / max(range_pct, 0.01)
        range_position = (current_close - low_price) / (high_price - low_price)

        return (
            -5 <= trend_return_pct <= 8
            and 5 <= range_pct <= 25
            and directional_efficiency <= 0.40
            and 0.20 <= range_position <= 0.80
        )

    def _build_alert(self, setup: GridSetup, candles: pd.DataFrame) -> dict[str, Any]:
        current_price = float(candles["close"].iloc[-1])
        low_price = current_price * (1 - setup.lower_pct / 100)
        high_price = current_price * (1 + setup.upper_pct / 100)
        grid_step_pct = (setup.lower_pct + setup.upper_pct) / setup.levels
        estimated_profit = self.config.investment_usdt * (setup.historical_monthly_pct / 100)
        return {
            "symbol": setup.symbol,
            "method_name": "Kraken GRID",
            "preset_name": setup.preset_name,
            "exchange": "Kraken",
            "investment_usdt": _fmt_number(self.config.investment_usdt),
            "low_price": _fmt_price(low_price),
            "high_price": _fmt_price(high_price),
            "grid_step_pct": _fmt_number(grid_step_pct),
            "levels": setup.levels,
            "order_size_currency": "USDT",
            "trailing_up": "On",
            "pump_protection": "On",
            "stop_loss_pct": f"-{_fmt_number(setup.stop_loss_pct)}",
            "take_profit_pct": f"+{_fmt_number(setup.take_profit_pct)}",
            "backtest_return_pct": f"+{_fmt_number(setup.historical_avg_return_pct)}",
            "backtest_days": "14",
            "estimated_profit_usdt": f"+{_fmt_number(estimated_profit)}",
            "max_drawdown_pct": f"-{_fmt_number(abs(setup.historical_worst_drawdown_pct))}",
            "score": setup.score,
            "historical_win_rate_pct": _fmt_number(setup.historical_win_rate_pct),
            "historical_alerts_per_month": _fmt_number(setup.historical_alerts_per_month),
        }

    def _in_cooldown(self, state_item: Any) -> bool:
        if not isinstance(state_item, dict):
            return False
        raw_last_alert = state_item.get("last_alert_at")
        if not raw_last_alert:
            return False
        try:
            last_alert_at = datetime.fromisoformat(str(raw_last_alert))
        except ValueError:
            return False
        if last_alert_at.tzinfo is None:
            last_alert_at = last_alert_at.replace(tzinfo=UTC)
        return datetime.now(UTC) - last_alert_at < timedelta(days=self.config.cooldown_days)

    @staticmethod
    def _state_key(setup: GridSetup) -> str:
        return f"{setup.symbol}|{setup.preset_name}|{setup.lower_pct}|{setup.upper_pct}|{setup.levels}"

    def _load_state(self) -> dict[str, Any]:
        if not self._state_path.exists():
            return {}
        try:
            with self._state_path.open("r", encoding="utf-8") as file:
                data = json.load(file)
            return data if isinstance(data, dict) else {}
        except Exception:
            LOGGER.exception("Failed to load GRID watch state")
            return {}

    def _save_state(self, state: dict[str, Any]) -> None:
        with self._state_path.open("w", encoding="utf-8") as file:
            json.dump(state, file, indent=2, sort_keys=True)


def _fmt_number(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _fmt_price(value: float) -> str:
    if value >= 100:
        return f"{value:.2f}"
    if value >= 1:
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return f"{value:.8f}".rstrip("0").rstrip(".")
