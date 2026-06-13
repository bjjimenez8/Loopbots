from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
    discovery_refresh_minutes: int = 60


class MarketDataClient:
    def __init__(self, config: MarketDataConfig) -> None:
        if not hasattr(ccxt, config.exchange_id):
            raise ValueError(f"Unsupported exchange id: {config.exchange_id}")

        exchange_class = getattr(ccxt, config.exchange_id)
        self.exchange = exchange_class({"enableRateLimit": config.enable_rate_limit})
        self.timeframe = config.timeframe
        self.candle_limit = config.candle_limit
        self.discovery_refresh_minutes = config.discovery_refresh_minutes
        self._markets_loaded = False
        self._tickers_cache: dict[str, Any] | None = None
        self._tickers_cache_at: datetime | None = None
        self._last_discovery_details: list[dict[str, Any]] = []

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

    def discover_pairs(self, discovery_config: dict[str, Any]) -> list[str]:
        self._ensure_markets_loaded()
        tickers = self._get_tickers()
        quote_asset = discovery_config.get("quote_asset", "USDT")
        hot_config = discovery_config.get("hot_discovery", {})
        hot_enabled = bool(hot_config.get("enabled", False))
        min_quote_volume = float(discovery_config.get("min_quote_volume_usdt", 2_000_000))
        min_last_price = float(discovery_config.get("min_last_price", 0.05))
        min_volatility_pct = float(discovery_config.get("min_volatility_pct", 2.5))
        max_volatility_pct = float(discovery_config.get("max_volatility_pct", 18.0))
        max_pairs = int(discovery_config.get("max_pairs", 12))
        max_abs_change_pct = float(hot_config.get("max_abs_change_pct", 18.0))
        ideal_min_volatility_pct = float(hot_config.get("ideal_min_volatility_pct", max(min_volatility_pct, 2.5)))
        ideal_max_volatility_pct = float(hot_config.get("ideal_max_volatility_pct", min(max_volatility_pct, 12.0)))
        hot_min_quote_volume = float(hot_config.get("min_quote_volume_usdt", min_quote_volume))
        hot_min_volatility_pct = float(hot_config.get("min_volatility_pct", min_volatility_pct))
        hot_max_volatility_pct = float(hot_config.get("max_volatility_pct", max_volatility_pct))
        hot_max_pairs = int(hot_config.get("max_pairs", max_pairs))
        auto_add_new = bool(hot_config.get("auto_add_new", True))
        excluded_bases = {base.upper() for base in discovery_config.get("excluded_base_assets", [])}
        excluded_bases.update(base.upper() for base in hot_config.get("excluded_base_assets", []))
        preferred_bases = [base.upper() for base in discovery_config.get("preferred_base_assets", [])]
        require_preferred_bases = bool(discovery_config.get("require_preferred_base_assets", False))
        strategy_watchlist_bases = {
            base.upper() for base in discovery_config.get("strategy_watchlist_base_assets", [])
        }
        watchlist_min_quote_volume = float(
            discovery_config.get("watchlist_min_quote_volume_usdt", min_quote_volume)
        )
        watchlist_min_volatility_pct = float(
            discovery_config.get("watchlist_min_volatility_pct", min_volatility_pct)
        )
        watchlist_max_volatility_pct = float(
            discovery_config.get("watchlist_max_volatility_pct", max_volatility_pct)
        )

        candidates: list[dict[str, Any]] = []
        for symbol, market in self.exchange.markets.items():
            if market.get("quote") != quote_asset:
                continue
            if not market.get("spot", False):
                continue
            if not market.get("active", True):
                continue

            base_asset = str(market.get("base", "")).upper()
            if base_asset in excluded_bases:
                continue
            if not base_asset or base_asset.endswith(".S"):
                continue
            if require_preferred_bases and base_asset not in preferred_bases:
                continue

            ticker = tickers.get(symbol) or {}
            last_price = self._coalesce_number(ticker.get("last"), ticker.get("close"), 0.0)
            quote_volume = self._quote_volume(ticker, last_price)
            high_price = self._coalesce_number(ticker.get("high"), last_price, 0.0)
            low_price = self._coalesce_number(ticker.get("low"), last_price, 0.0)
            change_pct = self._coalesce_number(ticker.get("percentage"), 0.0)
            is_watchlist = base_asset in strategy_watchlist_bases
            min_required_quote_volume = watchlist_min_quote_volume if is_watchlist else min_quote_volume
            if hot_enabled and auto_add_new and not is_watchlist:
                min_required_quote_volume = min(min_required_quote_volume, hot_min_quote_volume)

            if quote_volume < min_required_quote_volume:
                continue
            if last_price < min_last_price:
                continue
            if low_price <= 0:
                continue

            volatility_pct = ((high_price - low_price) / low_price) * 100 if low_price else 0.0
            min_required_volatility = watchlist_min_volatility_pct if is_watchlist else min_volatility_pct
            max_allowed_volatility = watchlist_max_volatility_pct if is_watchlist else max_volatility_pct
            if hot_enabled and auto_add_new and not is_watchlist:
                min_required_volatility = min(min_required_volatility, hot_min_volatility_pct)
                max_allowed_volatility = max(max_allowed_volatility, hot_max_volatility_pct)
            if volatility_pct < min_required_volatility or volatility_pct > max_allowed_volatility:
                continue
            if hot_enabled and abs(change_pct) > max_abs_change_pct and not is_watchlist:
                continue

            score = self._loop_hot_score(
                quote_volume=quote_volume,
                volatility_pct=volatility_pct,
                change_pct=change_pct,
                preferred=base_asset in preferred_bases,
                watchlist=is_watchlist,
                min_quote_volume=min_required_quote_volume,
                ideal_min_volatility_pct=ideal_min_volatility_pct,
                ideal_max_volatility_pct=ideal_max_volatility_pct,
                max_abs_change_pct=max_abs_change_pct,
            )
            candidates.append(
                {
                    "symbol": symbol,
                    "base": base_asset,
                    "last_price": last_price,
                    "quote_volume": quote_volume,
                    "volatility_pct": volatility_pct,
                    "change_pct": change_pct,
                    "preferred": base_asset in preferred_bases,
                    "watchlist": is_watchlist,
                    "score": score,
                    "reason": self._loop_hot_reason(volatility_pct, change_pct, is_watchlist),
                }
            )

        candidates.sort(
            key=lambda item: (
                not item["watchlist"],
                not item["preferred"],
                -item["score"],
                -item["volatility_pct"],
                -item["quote_volume"],
            )
        )
        limit = max(hot_max_pairs if hot_enabled else max_pairs, max_pairs)
        selected_candidates = candidates[:limit]
        selected = [item["symbol"] for item in selected_candidates[:max_pairs]]
        self._last_discovery_details = selected_candidates
        LOGGER.info("Discovered %d candidate pairs: %s", len(selected), ", ".join(selected))
        return selected

    def discovery_snapshot(self) -> list[dict[str, Any]]:
        return list(self._last_discovery_details)

    def _ensure_markets_loaded(self) -> None:
        if self._markets_loaded:
            return
        self.exchange.load_markets()
        self._markets_loaded = True

    def _get_tickers(self) -> dict[str, Any]:
        now = datetime.now(UTC)
        if (
            self._tickers_cache is not None
            and self._tickers_cache_at is not None
            and now - self._tickers_cache_at < timedelta(minutes=self.discovery_refresh_minutes)
        ):
            return self._tickers_cache

        self._ensure_markets_loaded()
        self._tickers_cache = self.exchange.fetch_tickers()
        self._tickers_cache_at = now
        return self._tickers_cache

    @staticmethod
    def _coalesce_number(*values: Any) -> float:
        for value in values:
            try:
                if value is None:
                    continue
                return float(value)
            except (TypeError, ValueError):
                continue
        return 0.0

    @classmethod
    def _quote_volume(cls, ticker: dict[str, Any], last_price: float) -> float:
        quote_volume = cls._coalesce_number(ticker.get("quoteVolume"))
        if quote_volume > 0:
            return quote_volume

        base_volume = cls._coalesce_number(ticker.get("baseVolume"))
        return base_volume * last_price if last_price > 0 else 0.0

    @staticmethod
    def _loop_hot_score(
        quote_volume: float,
        volatility_pct: float,
        change_pct: float,
        preferred: bool,
        watchlist: bool,
        min_quote_volume: float,
        ideal_min_volatility_pct: float,
        ideal_max_volatility_pct: float,
        max_abs_change_pct: float,
    ) -> int:
        volume_score = min(30.0, (quote_volume / max(min_quote_volume, 1.0)) * 10.0)
        if ideal_min_volatility_pct <= volatility_pct <= ideal_max_volatility_pct:
            volatility_score = 30.0
        else:
            ideal_midpoint = (ideal_min_volatility_pct + ideal_max_volatility_pct) / 2
            distance = abs(volatility_pct - ideal_midpoint)
            width = max(ideal_max_volatility_pct - ideal_min_volatility_pct, 0.01)
            volatility_score = max(0.0, 30.0 - (distance / width) * 20.0)

        abs_change = abs(change_pct)
        if abs_change <= max_abs_change_pct * 0.65:
            move_score = 20.0
        else:
            move_score = max(0.0, 20.0 - ((abs_change - max_abs_change_pct * 0.65) / max(max_abs_change_pct, 1.0)) * 30.0)

        priority_score = (12.0 if watchlist else 0.0) + (8.0 if preferred else 0.0)
        return int(round(max(0.0, min(100.0, volume_score + volatility_score + move_score + priority_score))))

    @staticmethod
    def _loop_hot_reason(volatility_pct: float, change_pct: float, watchlist: bool) -> str:
        parts = []
        if watchlist:
            parts.append("watchlist")
        if volatility_pct >= 8:
            parts.append("hot volatility")
        elif volatility_pct >= 3:
            parts.append("active")
        else:
            parts.append("quiet")
        if change_pct >= 0:
            parts.append(f"+{change_pct:.2f}% 24h")
        else:
            parts.append(f"{change_pct:.2f}% 24h")
        return ", ".join(parts)
