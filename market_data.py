from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import ccxt
import pandas as pd


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class MarketDataConfig:
    exchange_id: str
    enable_rate_limit: bool
    sandbox: bool
    timeframe: str
    candle_limit: int


class MarketDataClient:
    def __init__(self, config: MarketDataConfig) -> None:
        if not hasattr(ccxt, config.exchange_id):
            raise ValueError(f"Unsupported exchange id: {config.exchange_id}")

        exchange_class = getattr(ccxt, config.exchange_id)
        self.exchange = exchange_class({"enableRateLimit": config.enable_rate_limit})
        self.timeframe = config.timeframe
        self.candle_limit = config.candle_limit

        if config.sandbox and hasattr(self.exchange, "set_sandbox_mode"):
            self.exchange.set_sandbox_mode(True)

    def fetch_ohlcv(self, symbol: str) -> pd.DataFrame:
        LOGGER.debug("Fetching %s candles for %s", self.timeframe, symbol)
        rows: list[list[Any]] = self.exchange.fetch_ohlcv(
            symbol,
            timeframe=self.timeframe,
            limit=self.candle_limit,
        )

        if not rows:
            raise RuntimeError(f"No OHLCV data returned for {symbol}")

        df = pd.DataFrame(
            rows,
            columns=["timestamp", "open", "high", "low", "close", "volume"],
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        for column in ["open", "high", "low", "close", "volume"]:
            df[column] = pd.to_numeric(df[column], errors="coerce")

        return df.dropna().reset_index(drop=True)
