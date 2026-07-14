from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import ccxt
import pandas as pd


LOGGER = logging.getLogger(__name__)


STABLE_BASES = {
    "USDT",
    "USDC",
    "DAI",
    "PYUSD",
    "FDUSD",
    "TUSD",
    "USDE",
    "USDG",
    "EUR",
    "USD",
    "GBP",
    "AUD",
    "CAD",
    "JPY",
}

LEVERAGED_SUFFIXES = ("UP", "DOWN", "3L", "3S", "5L", "5S", "BULL", "BEAR")


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
    historical_starts: int
    historical_fee_pct: float
    historical_train_avg_return_pct: float
    historical_test_avg_return_pct: float
    historical_non_overlapping: bool
    score: int
    launch_filter: str = "strict-sideways"


@dataclass(frozen=True)
class HotGridProfile:
    base_asset: str
    allowed_quotes: list[str]
    preset_name: str
    lower_pct: float
    upper_pct: float
    levels: int
    take_profit_pct: float
    stop_loss_pct: float
    launch_filter: str
    historical_win_rate_pct: float
    historical_avg_return_pct: float
    historical_monthly_pct: float
    historical_avg_drawdown_pct: float
    historical_worst_drawdown_pct: float
    historical_alerts_per_month: float
    historical_starts: int
    historical_fee_pct: float
    historical_train_avg_return_pct: float
    historical_test_avg_return_pct: float
    historical_non_overlapping: bool
    score: int


@dataclass(frozen=True)
class HotGridDiscoveryConfig:
    enabled: bool
    quote_assets: list[str]
    min_quote_volume: float
    profile_min_quote_volume: float
    min_last_price: float
    min_volatility_pct: float
    max_volatility_pct: float
    max_abs_change_pct: float
    max_pairs: int
    profiles: list[HotGridProfile]
    auto_add_new: bool
    auto_min_quote_volume: float
    auto_min_volatility_pct: float
    auto_max_volatility_pct: float
    auto_max_abs_change_pct: float
    auto_preset_name: str
    auto_lower_pct: float
    auto_upper_pct: float
    auto_levels: int
    auto_take_profit_pct: float
    auto_stop_loss_pct: float
    auto_launch_filter: str
    auto_score: int
    excluded_base_assets: set[str]


@dataclass(frozen=True)
class GridWatchConfig:
    enabled: bool
    exchange_id: str
    timeframe: str
    candle_limit: int
    investment_usdt: float
    paper_fee_pct: float
    filter_lookback_days: float
    cooldown_days: float
    state_file: str
    history_file: str
    setups: list[GridSetup]
    hot_discovery: HotGridDiscoveryConfig


class GridWatchService:
    def __init__(self, config: GridWatchConfig) -> None:
        self.config = config
        if not hasattr(ccxt, config.exchange_id):
            raise ValueError(f"Unsupported GRID exchange id: {config.exchange_id}")
        exchange_class = getattr(ccxt, config.exchange_id)
        self.exchange = exchange_class({"enableRateLimit": True})
        self.exchange.load_markets()
        self._state_path = Path(config.state_file)
        self._history_path = Path(config.history_file)
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._history_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_history_file()

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
                historical_starts=int(item.get("historical_starts", 0)),
                historical_fee_pct=float(item.get("historical_fee_pct", 0.0)),
                historical_train_avg_return_pct=float(item.get("historical_train_avg_return_pct", 0.0)),
                historical_test_avg_return_pct=float(item.get("historical_test_avg_return_pct", 0.0)),
                historical_non_overlapping=bool(item.get("historical_non_overlapping", False)),
                score=int(item["score"]),
                launch_filter=str(item.get("launch_filter", "strict-sideways")),
            )
            for item in raw_config.get("setups", [])
        ]
        hot_raw = raw_config.get("hot_discovery", {})
        hot_profiles = [
            HotGridProfile(
                base_asset=str(item["base_asset"]).upper(),
                allowed_quotes=[str(quote).upper() for quote in item.get("allowed_quotes", hot_raw.get("quote_assets", ["USD", "USDC"]))],
                preset_name=str(item["preset_name"]),
                lower_pct=float(item["lower_pct"]),
                upper_pct=float(item["upper_pct"]),
                levels=int(item["levels"]),
                take_profit_pct=float(item.get("take_profit_pct", hot_raw.get("take_profit_pct", raw_config.get("take_profit_pct", 8.0)))),
                stop_loss_pct=float(item.get("stop_loss_pct", hot_raw.get("stop_loss_pct", raw_config.get("stop_loss_pct", 5.0)))),
                launch_filter=str(item.get("launch_filter", "sideways")),
                historical_win_rate_pct=float(item["historical_win_rate_pct"]),
                historical_avg_return_pct=float(item["historical_avg_return_pct"]),
                historical_monthly_pct=float(item["historical_monthly_pct"]),
                historical_avg_drawdown_pct=float(item["historical_avg_drawdown_pct"]),
                historical_worst_drawdown_pct=float(item["historical_worst_drawdown_pct"]),
                historical_alerts_per_month=float(item["historical_alerts_per_month"]),
                historical_starts=int(item.get("historical_starts", 0)),
                historical_fee_pct=float(item.get("historical_fee_pct", 0.0)),
                historical_train_avg_return_pct=float(item.get("historical_train_avg_return_pct", 0.0)),
                historical_test_avg_return_pct=float(item.get("historical_test_avg_return_pct", 0.0)),
                historical_non_overlapping=bool(item.get("historical_non_overlapping", False)),
                score=int(item["score"]),
            )
            for item in hot_raw.get("profiles", [])
        ]
        hot_discovery = HotGridDiscoveryConfig(
            enabled=bool(hot_raw.get("enabled", False)),
            quote_assets=[str(quote).upper() for quote in hot_raw.get("quote_assets", ["USD", "USDC"])],
            min_quote_volume=float(hot_raw.get("min_quote_volume", 100_000.0)),
            profile_min_quote_volume=float(hot_raw.get("profile_min_quote_volume", hot_raw.get("min_quote_volume", 100_000.0))),
            min_last_price=float(hot_raw.get("min_last_price", 0.000001)),
            min_volatility_pct=float(hot_raw.get("min_volatility_pct", 2.0)),
            max_volatility_pct=float(hot_raw.get("max_volatility_pct", 80.0)),
            max_abs_change_pct=float(hot_raw.get("max_abs_change_pct", 35.0)),
            max_pairs=int(hot_raw.get("max_pairs", 8)),
            profiles=hot_profiles,
            auto_add_new=bool(hot_raw.get("auto_add_new", False)),
            auto_min_quote_volume=float(hot_raw.get("auto_min_quote_volume", 500_000.0)),
            auto_min_volatility_pct=float(hot_raw.get("auto_min_volatility_pct", 3.0)),
            auto_max_volatility_pct=float(hot_raw.get("auto_max_volatility_pct", 35.0)),
            auto_max_abs_change_pct=float(hot_raw.get("auto_max_abs_change_pct", 20.0)),
            auto_preset_name=str(hot_raw.get("auto_preset_name", "Auto Hot GRID")),
            auto_lower_pct=float(hot_raw.get("auto_lower_pct", 8.0)),
            auto_upper_pct=float(hot_raw.get("auto_upper_pct", 35.0)),
            auto_levels=int(hot_raw.get("auto_levels", 10)),
            auto_take_profit_pct=float(hot_raw.get("auto_take_profit_pct", hot_raw.get("take_profit_pct", raw_config.get("take_profit_pct", 8.0)))),
            auto_stop_loss_pct=float(hot_raw.get("auto_stop_loss_pct", hot_raw.get("stop_loss_pct", raw_config.get("stop_loss_pct", 5.0)))),
            auto_launch_filter=str(hot_raw.get("auto_launch_filter", "strict-sideways")),
            auto_score=int(hot_raw.get("auto_score", 65)),
            excluded_base_assets={
                str(base).upper()
                for base in hot_raw.get("excluded_base_assets", [])
            },
        )
        state_file = str(project_root / raw_config.get("state_file", "data/grid_watch_state.json"))
        history_file = str(project_root / raw_config.get("history_file", "data/grid_trade_history.csv"))
        return cls(
            GridWatchConfig(
                enabled=bool(raw_config.get("enabled", False)),
                exchange_id=str(raw_config.get("exchange_id", "kraken")),
                timeframe=str(raw_config.get("timeframe", "1h")),
                candle_limit=int(raw_config.get("candle_limit", 360)),
                investment_usdt=float(raw_config.get("investment_usdt", 5000.0)),
                paper_fee_pct=float(raw_config.get("paper_fee_pct", 0.2)),
                filter_lookback_days=float(raw_config.get("filter_lookback_days", 14.0)),
                cooldown_days=float(raw_config.get("cooldown_days", 14.0)),
                state_file=state_file,
                history_file=history_file,
                setups=setups,
                hot_discovery=hot_discovery,
            )
        )

    def find_alerts(self) -> list[dict[str, Any]]:
        if not self.config.enabled:
            return []

        state = self._load_state()
        alerts: list[dict[str, Any]] = []
        for setup in self._candidate_setups():
            if not self._has_current_proof(setup):
                continue
            key = self._state_key(setup)
            state_item = state.get(key)
            if self._in_cooldown(state_item):
                continue

            try:
                candles = self._fetch_candles(setup.symbol)
            except Exception:
                LOGGER.exception("Failed to fetch GRID candles for %s", setup.symbol)
                continue

            if not self._passes_sideways(candles, setup.launch_filter):
                continue

            alert = self._build_alert(setup, candles)
            alerts.append(alert)
            now = datetime.now(UTC).isoformat()
            state[key] = {
                "last_alert_at": now,
                "symbol": setup.symbol,
                "preset_name": setup.preset_name,
                "active_paper": self._build_active_paper(alert, now),
            }
            self._append_history(self._build_entry_history_row(alert, now))

        if alerts:
            self._save_state(state)
        return alerts

    def update_paper_trades(self) -> list[dict[str, Any]]:
        if not self.config.enabled:
            return []

        state = self._load_state()
        closed: list[dict[str, Any]] = []
        changed = False
        for key, state_item in state.items():
            if not isinstance(state_item, dict):
                continue
            active = state_item.get("active_paper")
            if not isinstance(active, dict) or active.get("status") != "ACTIVE":
                continue

            symbol = str(active.get("symbol") or state_item.get("symbol") or "")
            if not symbol:
                continue

            try:
                candles = self._fetch_candles(symbol)
            except Exception:
                LOGGER.exception("Failed to update GRID paper trade for %s", symbol)
                continue

            close_row = self._paper_close_row(active, candles)
            if close_row is None:
                latest_timestamp = _latest_timestamp(candles)
                if latest_timestamp:
                    active["last_checked_timestamp"] = latest_timestamp
                    state_item["active_paper"] = active
                    state[key] = state_item
                    changed = True
                continue

            state_item["active_paper"] = {
                **active,
                "status": "CLOSED",
                "closed_at": close_row["event_at"],
                "exit_price": close_row["exit_price"],
                "exit_reason": close_row["exit_reason"],
            }
            state[key] = state_item
            self._append_history(close_row)
            closed.append(close_row)
            changed = True

        if changed:
            self._save_state(state)
        return closed

    def diagnostics(self) -> list[dict[str, Any]]:
        if not self.config.enabled:
            return []

        state = self._load_state()
        results: list[dict[str, Any]] = []
        for setup in self._candidate_setups():
            key = self._state_key(setup)
            state_item = state.get(key)
            cooldown = self._in_cooldown(state_item)
            try:
                candles = self._fetch_candles(setup.symbol)
                status = self._sideways_status(candles, setup.launch_filter)
            except Exception as exc:
                LOGGER.exception("Failed to build GRID diagnostics for %s", setup.symbol)
                results.append(
                    {
                        "symbol": setup.symbol,
                        "ready": False,
                        "active": False,
                        "cooldown": cooldown,
                        "reason": f"data error: {exc}",
                    }
                )
                continue

            reasons = []
            if cooldown:
                reasons.append("cooldown")
            reasons.extend(status["reasons"])
            proof_ready = self._has_current_proof(setup)
            if not proof_ready:
                reasons.append("needs 30 non-overlapping realistic-fee train/test starts")
            ready = not cooldown and status["passes"] and proof_ready
            results.append(
                {
                    "symbol": setup.symbol,
                    "preset_name": setup.preset_name,
                    "lower_pct": setup.lower_pct,
                    "upper_pct": setup.upper_pct,
                    "levels": setup.levels,
                    "grid_step_pct": _setup_grid_step_pct(setup.lower_pct, setup.upper_pct, setup.levels),
                    "take_profit_pct": setup.take_profit_pct,
                    "stop_loss_pct": setup.stop_loss_pct,
                    "launch_filter": setup.launch_filter,
                    "ready": ready,
                    "active": False,
                    "cooldown": cooldown,
                    "reason": "READY" if ready else "; ".join(reasons),
                    "historical_win_rate_pct": setup.historical_win_rate_pct,
                    "historical_avg_return_pct": setup.historical_avg_return_pct,
                    "historical_monthly_pct": setup.historical_monthly_pct,
                    "historical_worst_drawdown_pct": setup.historical_worst_drawdown_pct,
                    "historical_starts": setup.historical_starts,
                    "historical_fee_pct": setup.historical_fee_pct,
                    "historical_train_avg_return_pct": setup.historical_train_avg_return_pct,
                    "historical_test_avg_return_pct": setup.historical_test_avg_return_pct,
                    "historical_non_overlapping": setup.historical_non_overlapping,
                    "experimental": "experimental" in setup.preset_name.lower(),
                    **status,
                }
            )
        return results

    @staticmethod
    def _has_current_proof(setup: GridSetup) -> bool:
        return bool(
            setup.historical_starts >= 30
            and setup.historical_fee_pct >= 0.25
            and setup.historical_train_avg_return_pct > 0
            and setup.historical_test_avg_return_pct > 0
            and setup.historical_non_overlapping
        )

    def paper_snapshot(self, include_diagnostics: bool = False) -> dict[str, Any]:
        rows = self._read_history()
        closed_rows = [row for row in rows if row.get("event") in {"GRID_TAKE_PROFIT", "GRID_STOP_LOSS"}]
        state = self._load_state()
        active_trades = self._active_paper_records(state)
        closed_trades = [self._closed_paper_record(row) for row in closed_rows]
        closed_trades.sort(key=lambda row: row.get("event_at", ""), reverse=True)
        wins = sum(1 for row in closed_rows if row.get("event") == "GRID_TAKE_PROFIT")
        losses = sum(1 for row in closed_rows if row.get("event") == "GRID_STOP_LOSS")
        net_returns = [_to_float(row.get("net_return_pct")) for row in closed_rows]
        snapshot = {
            "entries": sum(1 for row in rows if row.get("event") == "GRID_ENTRY"),
            "closed": len(closed_rows),
            "active": len(active_trades),
            "wins": wins,
            "losses": losses,
            "win_rate_pct": (wins / len(closed_rows) * 100) if closed_rows else 0.0,
            "net_return_pct": sum(net_returns),
            "avg_net_return_pct": (sum(net_returns) / len(closed_rows)) if closed_rows else 0.0,
            "active_trades": active_trades,
            "closed_trades": closed_trades,
        }
        if include_diagnostics:
            snapshot["scanned"] = self._grid_scan_records(state)
        return snapshot

    def _active_paper_records(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        records = []
        for item in state.values():
            if not isinstance(item, dict) or not isinstance(item.get("active_paper"), dict):
                continue
            active = item["active_paper"]
            if active.get("status") != "ACTIVE":
                continue
            records.append(
                {
                    "symbol": active.get("symbol", ""),
                    "preset_name": active.get("preset_name", ""),
                    "entry_price": _to_float(active.get("entry_price")),
                    "take_profit_price": _to_float(active.get("take_profit_price")),
                    "stop_loss_price": _to_float(active.get("stop_loss_price")),
                    "grid_step_pct": active.get("grid_step_pct", ""),
                    "levels": active.get("levels", ""),
                    "opened_at": active.get("opened_at", ""),
                }
            )
        records.sort(key=lambda row: row.get("opened_at", ""), reverse=True)
        return records

    @staticmethod
    def _closed_paper_record(row: dict[str, str]) -> dict[str, Any]:
        return {
            "event": row.get("event", ""),
            "symbol": row.get("symbol", ""),
            "preset_name": row.get("preset_name", ""),
            "entry_price": _to_float(row.get("entry_price")),
            "exit_price": _to_float(row.get("exit_price")),
            "take_profit_price": _to_float(row.get("take_profit_price")),
            "stop_loss_price": _to_float(row.get("stop_loss_price")),
            "grid_step_pct": row.get("grid_step_pct", ""),
            "levels": row.get("levels", ""),
            "net_return_pct": _to_float(row.get("net_return_pct")),
            "opened_at": row.get("opened_at", ""),
            "event_at": row.get("event_at", ""),
            "exit_reason": row.get("exit_reason", ""),
        }

    def _grid_scan_records(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        active_symbols = {
            str(item.get("active_paper", {}).get("symbol", ""))
            for item in state.values()
            if isinstance(item, dict)
            and isinstance(item.get("active_paper"), dict)
            and item["active_paper"].get("status") == "ACTIVE"
        }
        try:
            rows = self.diagnostics()
        except Exception:
            LOGGER.exception("Failed to build GRID dashboard diagnostics")
            return []

        records = []
        for row in rows:
            records.append(
                {
                    "symbol": row.get("symbol", ""),
                    "preset_name": row.get("preset_name", ""),
                    "score": int(_to_float(row.get("score"))),
                    "ready": bool(row.get("ready")),
                    "active": str(row.get("symbol", "")) in active_symbols,
                    "cooldown": bool(row.get("cooldown")),
                    "current_price": _to_float(row.get("current_price")),
                    "reason": row.get("reason", ""),
                    "lower_pct": _to_float(row.get("lower_pct")),
                    "upper_pct": _to_float(row.get("upper_pct")),
                    "levels": int(_to_float(row.get("levels"))),
                    "grid_step_pct": _to_float(row.get("grid_step_pct")),
                    "take_profit_pct": _to_float(row.get("take_profit_pct")),
                    "stop_loss_pct": _to_float(row.get("stop_loss_pct")),
                    "launch_filter": row.get("launch_filter", ""),
                    "trend_return_pct": _to_float(row.get("trend_return_pct")),
                    "range_pct": _to_float(row.get("range_pct")),
                    "directional_efficiency": _to_float(row.get("directional_efficiency")),
                    "range_position": _to_float(row.get("range_position")),
                    "historical_win_rate_pct": _to_float(row.get("historical_win_rate_pct")),
                    "historical_avg_return_pct": _to_float(row.get("historical_avg_return_pct")),
                    "historical_monthly_pct": _to_float(row.get("historical_monthly_pct")),
                    "historical_worst_drawdown_pct": _to_float(row.get("historical_worst_drawdown_pct")),
                    "experimental": bool(row.get("experimental")),
                }
            )
        records.sort(key=lambda row: (not row["ready"], -row["score"], row["symbol"]))
        return records

    def _fetch_candles(self, symbol: str) -> pd.DataFrame:
        rows = self.exchange.fetch_ohlcv(symbol, timeframe=self.config.timeframe, limit=self.config.candle_limit)
        if not rows:
            raise RuntimeError(f"No GRID candles returned for {symbol}")

        df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        for column in ["open", "high", "low", "close", "volume"]:
            df[column] = pd.to_numeric(df[column], errors="coerce")
        return df.dropna().reset_index(drop=True)

    def _candidate_setups(self) -> list[GridSetup]:
        setups = list(self.config.setups)
        if self.config.hot_discovery.enabled:
            setups.extend(self._discover_hot_setups())
        seen: set[str] = set()
        unique: list[GridSetup] = []
        for setup in sorted(setups, key=lambda item: (-item.score, item.symbol)):
            key = self._state_key(setup)
            if key in seen:
                continue
            seen.add(key)
            unique.append(setup)
        return unique

    def _discover_hot_setups(self) -> list[GridSetup]:
        try:
            tickers = self.exchange.fetch_tickers()
        except Exception:
            LOGGER.exception("Failed to fetch GRID hot-discovery tickers")
            return []

        candidates: list[tuple[GridSetup, float, float]] = []
        for symbol, market in self.exchange.markets.items():
            quote = str(market.get("quote", "")).upper()
            if quote not in self.config.hot_discovery.quote_assets:
                continue
            if not market.get("spot", False) or not market.get("active", True):
                continue

            base = str(market.get("base", "")).upper()
            ticker = tickers.get(symbol) or {}
            last_price = _number(ticker.get("last"), ticker.get("close"), 0.0)
            high_price = _number(ticker.get("high"), last_price, 0.0)
            low_price = _number(ticker.get("low"), last_price, 0.0)
            quote_volume = _quote_volume(ticker, last_price)
            volatility_pct = ((high_price - low_price) / low_price) * 100 if low_price > 0 else 0.0
            change_pct = abs(_number(ticker.get("percentage"), 0.0))

            if last_price < self.config.hot_discovery.min_last_price:
                continue
            min_quote_volume = self.config.hot_discovery.min_quote_volume
            if quote_volume < min_quote_volume:
                continue
            if volatility_pct < self.config.hot_discovery.min_volatility_pct:
                continue
            if volatility_pct > self.config.hot_discovery.max_volatility_pct:
                continue
            if change_pct > self.config.hot_discovery.max_abs_change_pct:
                continue

            setup = self._auto_setup(
                symbol=symbol,
                base=base,
                quote_volume=quote_volume,
                volatility_pct=volatility_pct,
                change_pct=change_pct,
            )
            if setup is None:
                continue

            candidates.append(
                (
                    setup,
                    quote_volume,
                    volatility_pct,
                )
            )

        candidates.sort(key=lambda item: (-item[0].score, -item[1], -item[2], item[0].symbol))
        return [setup for setup, _, _ in candidates[: self.config.hot_discovery.max_pairs]]

    @staticmethod
    def _profile_setup(symbol: str, profile: HotGridProfile) -> GridSetup:
        return GridSetup(
            symbol=symbol,
            preset_name=profile.preset_name,
            lower_pct=profile.lower_pct,
            upper_pct=profile.upper_pct,
            levels=profile.levels,
            take_profit_pct=profile.take_profit_pct,
            stop_loss_pct=profile.stop_loss_pct,
            historical_win_rate_pct=profile.historical_win_rate_pct,
            historical_avg_return_pct=profile.historical_avg_return_pct,
            historical_monthly_pct=profile.historical_monthly_pct,
            historical_avg_drawdown_pct=profile.historical_avg_drawdown_pct,
            historical_worst_drawdown_pct=profile.historical_worst_drawdown_pct,
            historical_alerts_per_month=profile.historical_alerts_per_month,
            historical_starts=profile.historical_starts,
            historical_fee_pct=profile.historical_fee_pct,
            historical_train_avg_return_pct=profile.historical_train_avg_return_pct,
            historical_test_avg_return_pct=profile.historical_test_avg_return_pct,
            historical_non_overlapping=profile.historical_non_overlapping,
            score=profile.score,
            launch_filter=profile.launch_filter,
        )

    def _auto_setup(
        self,
        symbol: str,
        base: str,
        quote_volume: float,
        volatility_pct: float,
        change_pct: float,
    ) -> GridSetup | None:
        hot = self.config.hot_discovery
        if not hot.auto_add_new:
            return None
        if base in STABLE_BASES or base in hot.excluded_base_assets:
            return None
        if base.endswith(LEVERAGED_SUFFIXES) or not _is_plain_symbol(base):
            return None
        if quote_volume < hot.auto_min_quote_volume:
            return None
        if volatility_pct < hot.auto_min_volatility_pct or volatility_pct > hot.auto_max_volatility_pct:
            return None
        if change_pct > hot.auto_max_abs_change_pct:
            return None

        lower_pct = _clamp(volatility_pct * 0.35, 5.0, 12.0)
        upper_pct = _clamp(volatility_pct * 1.25, 18.0, 50.0)
        target_step_pct = _clamp(volatility_pct / 3.0, 3.0, 7.0)
        levels = int(_clamp(round((lower_pct + upper_pct) / target_step_pct), 5, 100))
        score = _grid_hot_score(
            quote_volume=quote_volume,
            volatility_pct=volatility_pct,
            change_pct=change_pct,
            min_quote_volume=hot.auto_min_quote_volume,
            min_volatility_pct=hot.auto_min_volatility_pct,
            max_volatility_pct=hot.auto_max_volatility_pct,
            max_abs_change_pct=hot.auto_max_abs_change_pct,
        )
        return GridSetup(
            symbol=symbol,
            preset_name=hot.auto_preset_name,
            lower_pct=round(lower_pct, 2),
            upper_pct=round(upper_pct, 2),
            levels=levels,
            take_profit_pct=hot.auto_take_profit_pct,
            stop_loss_pct=hot.auto_stop_loss_pct,
            historical_win_rate_pct=0.0,
            historical_avg_return_pct=0.0,
            historical_monthly_pct=0.0,
            historical_avg_drawdown_pct=0.0,
            historical_worst_drawdown_pct=0.0,
            historical_alerts_per_month=0.0,
            historical_starts=0,
            historical_fee_pct=0.0,
            historical_train_avg_return_pct=0.0,
            historical_test_avg_return_pct=0.0,
            historical_non_overlapping=False,
            score=score,
            launch_filter=hot.auto_launch_filter,
        )

    def _passes_sideways(self, candles: pd.DataFrame, launch_filter: str) -> bool:
        return bool(self._sideways_status(candles, launch_filter)["passes"])

    def _sideways_status(self, candles: pd.DataFrame, launch_filter: str = "strict-sideways") -> dict[str, Any]:
        if candles.empty:
            return {"passes": False, "reasons": ["no candles"]}

        cutoff = candles["timestamp"].iloc[-1] - pd.Timedelta(days=self.config.filter_lookback_days)
        lookback = candles[candles["timestamp"] >= cutoff]
        if len(lookback) < 20:
            return {"passes": False, "reasons": ["not enough candles"]}

        first_close = float(lookback["close"].iloc[0])
        current_close = float(lookback["close"].iloc[-1])
        high_price = float(lookback["high"].max())
        low_price = float(lookback["low"].min())
        if first_close <= 0 or current_close <= 0 or low_price <= 0 or high_price <= low_price:
            return {"passes": False, "reasons": ["invalid range"]}

        trend_return_pct = ((current_close / first_close) - 1) * 100
        range_pct = ((high_price / low_price) - 1) * 100
        directional_efficiency = abs(trend_return_pct) / max(range_pct, 0.01)
        range_position = (current_close - low_price) / (high_price - low_price)
        if launch_filter == "sideways":
            min_trend_pct = -8.0
            max_trend_pct = 12.0
            min_range_pct = 5.0
            max_range_pct = 35.0
            max_directional_efficiency = 0.55
            min_range_position = 0.15
            max_range_position = 0.90
        else:
            min_trend_pct = -5.0
            max_trend_pct = 8.0
            min_range_pct = 5.0
            max_range_pct = 25.0
            max_directional_efficiency = 0.40
            min_range_position = 0.20
            max_range_position = 0.80

        reasons = []
        if not min_trend_pct <= trend_return_pct <= max_trend_pct:
            reasons.append(f"trend {trend_return_pct:.2f}%")
        if not min_range_pct <= range_pct <= max_range_pct:
            reasons.append(f"range {range_pct:.2f}%")
        if directional_efficiency > max_directional_efficiency:
            reasons.append(f"directional {directional_efficiency:.2f}")
        if not min_range_position <= range_position <= max_range_position:
            reasons.append(f"position {range_position:.2f}")
        return {
            "passes": not reasons,
            "reasons": reasons,
            "score": _grid_readiness_score(
                trend_return_pct=trend_return_pct,
                range_pct=range_pct,
                directional_efficiency=directional_efficiency,
                range_position=range_position,
                launch_filter=launch_filter,
            ),
            "trend_return_pct": round(trend_return_pct, 2),
            "range_pct": round(range_pct, 2),
            "directional_efficiency": round(directional_efficiency, 2),
            "range_position": round(range_position, 2),
            "current_price": current_close,
        }

    def _build_alert(self, setup: GridSetup, candles: pd.DataFrame) -> dict[str, Any]:
        current_price = float(candles["close"].iloc[-1])
        low_price = current_price * (1 - setup.lower_pct / 100)
        high_price = current_price * (1 + setup.upper_pct / 100)
        stop_loss_price = current_price * (1 - setup.stop_loss_pct / 100)
        take_profit_price = current_price * (1 + setup.take_profit_pct / 100)
        grid_step_pct = (((high_price / low_price) ** (1 / setup.levels)) - 1) * 100
        estimated_profit = self.config.investment_usdt * (setup.historical_monthly_pct / 100)
        return {
            "symbol": setup.symbol,
            "method_name": "Kraken GRID",
            "preset_name": setup.preset_name,
            "exchange": "Kraken",
            "entry_price": current_price,
            "investment_usdt": _fmt_number(self.config.investment_usdt),
            "low_price": _fmt_price(low_price),
            "high_price": _fmt_price(high_price),
            "take_profit_price": take_profit_price,
            "stop_loss_price": stop_loss_price,
            "signal_candle_timestamp": _timestamp_to_iso(candles["timestamp"].iloc[-1]),
            "grid_step_pct": _fmt_grid_step(grid_step_pct),
            "levels": setup.levels,
            "order_size_currency": setup.symbol.split("/")[-1],
            "trailing_up": "On",
            "pump_protection": "On",
            "trailing_down": "Off",
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

    def _ensure_history_file(self) -> None:
        if self._history_path.exists():
            self._migrate_history_file()
            return
        with self._history_path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=self._history_fields)
            writer.writeheader()

    def _migrate_history_file(self) -> None:
        with self._history_path.open("r", newline="", encoding="utf-8") as file:
            reader = csv.DictReader(file)
            if reader.fieldnames == self._history_fields:
                return
            rows = list(reader)

        with self._history_path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=self._history_fields)
            writer.writeheader()
            for row in rows:
                writer.writerow({field: row.get(field, "") for field in self._history_fields})

    def _append_history(self, row: dict[str, Any]) -> None:
        normalized = {field: row.get(field, "") for field in self._history_fields}
        with self._history_path.open("a", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=self._history_fields)
            writer.writerow(normalized)

    def _read_history(self) -> list[dict[str, str]]:
        if not self._history_path.exists():
            return []
        with self._history_path.open("r", newline="", encoding="utf-8") as file:
            return list(csv.DictReader(file))

    @property
    def _history_fields(self) -> list[str]:
        return [
            "event",
            "event_at",
            "symbol",
            "preset_name",
            "exchange",
            "entry_price",
            "exit_price",
            "low_price",
            "high_price",
            "take_profit_price",
            "stop_loss_price",
            "grid_step_pct",
            "levels",
            "take_profit_pct",
            "stop_loss_pct",
            "gross_return_pct",
            "net_return_pct",
            "opened_at",
            "exit_reason",
        ]

    @staticmethod
    def _build_entry_history_row(alert: dict[str, Any], event_at: str) -> dict[str, Any]:
        return {
            "event": "GRID_ENTRY",
            "event_at": event_at,
            "symbol": alert.get("symbol", ""),
            "preset_name": alert.get("preset_name", ""),
            "exchange": alert.get("exchange", ""),
            "entry_price": alert.get("entry_price", ""),
            "low_price": alert.get("low_price", ""),
            "high_price": alert.get("high_price", ""),
            "take_profit_price": alert.get("take_profit_price", ""),
            "stop_loss_price": alert.get("stop_loss_price", ""),
            "grid_step_pct": alert.get("grid_step_pct", ""),
            "levels": alert.get("levels", ""),
            "take_profit_pct": alert.get("take_profit_pct", ""),
            "stop_loss_pct": alert.get("stop_loss_pct", ""),
            "opened_at": event_at,
        }

    @staticmethod
    def _build_active_paper(alert: dict[str, Any], opened_at: str) -> dict[str, Any]:
        signal_timestamp = str(alert.get("signal_candle_timestamp") or "")
        return {
            "status": "ACTIVE",
            "symbol": alert.get("symbol", ""),
            "preset_name": alert.get("preset_name", ""),
            "exchange": alert.get("exchange", ""),
            "entry_price": alert.get("entry_price", ""),
            "low_price": alert.get("low_price", ""),
            "high_price": alert.get("high_price", ""),
            "take_profit_price": alert.get("take_profit_price", ""),
            "stop_loss_price": alert.get("stop_loss_price", ""),
            "grid_step_pct": alert.get("grid_step_pct", ""),
            "levels": alert.get("levels", ""),
            "take_profit_pct": alert.get("take_profit_pct", ""),
            "stop_loss_pct": alert.get("stop_loss_pct", ""),
            "opened_at": opened_at,
            "last_checked_timestamp": signal_timestamp,
        }

    def _paper_close_row(self, active: dict[str, Any], candles: pd.DataFrame) -> dict[str, Any] | None:
        last_checked = _parse_timestamp(active.get("last_checked_timestamp"))
        if last_checked is None:
            last_checked = _parse_timestamp(active.get("opened_at"))

        check_rows = candles
        if last_checked is not None:
            check_rows = candles[candles["timestamp"] > last_checked]
        if check_rows.empty:
            return None

        take_profit_price = _to_float(active.get("take_profit_price"))
        stop_loss_price = _to_float(active.get("stop_loss_price"))
        if take_profit_price <= 0 or stop_loss_price <= 0:
            return None

        for _, candle in check_rows.iterrows():
            exit_event = self._paper_exit_event(candle, take_profit_price, stop_loss_price)
            if exit_event is None:
                continue

            event, exit_price, exit_reason = exit_event
            entry_price = _to_float(active.get("entry_price"))
            gross_return_pct = ((exit_price / entry_price) - 1) * 100 if entry_price > 0 else 0.0
            net_return_pct = gross_return_pct - self.config.paper_fee_pct
            return {
                "event": event,
                "event_at": _timestamp_to_iso(candle["timestamp"]),
                "symbol": active.get("symbol", ""),
                "preset_name": active.get("preset_name", ""),
                "exchange": active.get("exchange", ""),
                "entry_price": active.get("entry_price", ""),
                "exit_price": exit_price,
                "low_price": active.get("low_price", ""),
                "high_price": active.get("high_price", ""),
                "take_profit_price": active.get("take_profit_price", ""),
                "stop_loss_price": active.get("stop_loss_price", ""),
                "grid_step_pct": active.get("grid_step_pct", ""),
                "levels": active.get("levels", ""),
                "take_profit_pct": active.get("take_profit_pct", ""),
                "stop_loss_pct": active.get("stop_loss_pct", ""),
                "gross_return_pct": round(gross_return_pct, 4),
                "net_return_pct": round(net_return_pct, 4),
                "opened_at": active.get("opened_at", ""),
                "exit_reason": exit_reason,
            }
        return None

    @staticmethod
    def _paper_exit_event(
        candle: pd.Series,
        take_profit_price: float,
        stop_loss_price: float,
    ) -> tuple[str, float, str] | None:
        high_price = float(candle["high"])
        low_price = float(candle["low"])
        if low_price <= stop_loss_price and high_price >= take_profit_price:
            return "GRID_STOP_LOSS", stop_loss_price, "paper stop loss touched first by conservative rule"
        if low_price <= stop_loss_price:
            return "GRID_STOP_LOSS", stop_loss_price, "paper stop loss touched"
        if high_price >= take_profit_price:
            return "GRID_TAKE_PROFIT", take_profit_price, "paper take profit touched"
        return None


def _fmt_number(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _fmt_grid_step(value: float) -> str:
    rounded_one_decimal = round(value, 1)
    if rounded_one_decimal.is_integer():
        return str(int(rounded_one_decimal))
    return f"{rounded_one_decimal:.1f}"


def _setup_grid_step_pct(lower_pct: float, upper_pct: float, levels: int) -> float:
    if levels <= 0:
        return 0.0
    low_ratio = 1 - (lower_pct / 100)
    high_ratio = 1 + (upper_pct / 100)
    if low_ratio <= 0 or high_ratio <= low_ratio:
        return 0.0
    return round(((high_ratio / low_ratio) ** (1 / levels) - 1) * 100, 2)


def _fmt_price(value: float) -> str:
    if value >= 100:
        return f"{value:.2f}"
    if value >= 1:
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return f"{value:.8f}".rstrip("0").rstrip(".")


def _number(*values: Any) -> float:
    for value in values:
        try:
            if value is None:
                continue
            number = float(value)
            if pd.notna(number):
                return number
        except (TypeError, ValueError):
            continue
    return 0.0


def _quote_volume(ticker: dict[str, Any], last_price: float) -> float:
    quote_volume = _number(ticker.get("quoteVolume"))
    if quote_volume > 0:
        return quote_volume
    base_volume = _number(ticker.get("baseVolume"))
    return base_volume * last_price if base_volume > 0 and last_price > 0 else 0.0


def _latest_timestamp(candles: pd.DataFrame) -> str:
    if candles.empty:
        return ""
    return _timestamp_to_iso(candles["timestamp"].iloc[-1])


def _timestamp_to_iso(value: Any) -> str:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC").isoformat()


def _parse_timestamp(value: Any) -> pd.Timestamp | None:
    if not value:
        return None
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(timestamp):
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def _grid_readiness_score(
    trend_return_pct: float,
    range_pct: float,
    directional_efficiency: float,
    range_position: float,
    launch_filter: str,
) -> int:
    if launch_filter == "sideways":
        min_trend_pct, max_trend_pct = -8.0, 12.0
        min_range_pct, max_range_pct = 5.0, 35.0
        max_directional_efficiency = 0.55
        min_range_position, max_range_position = 0.15, 0.90
    else:
        min_trend_pct, max_trend_pct = -5.0, 8.0
        min_range_pct, max_range_pct = 5.0, 25.0
        max_directional_efficiency = 0.40
        min_range_position, max_range_position = 0.20, 0.80

    trend_score = _range_score(trend_return_pct, min_trend_pct, max_trend_pct)
    range_score = _range_score(range_pct, min_range_pct, max_range_pct)
    directional_score = max(0.0, min(1.0, 1 - (directional_efficiency / max_directional_efficiency)))
    position_score = _range_score(range_position, min_range_position, max_range_position)
    total = (
        trend_score * 35
        + range_score * 25
        + directional_score * 20
        + position_score * 20
    )
    return int(round(max(0.0, min(100.0, total))))


def _range_score(value: float, minimum: float, maximum: float) -> float:
    if minimum <= value <= maximum:
        midpoint = (minimum + maximum) / 2
        half_width = max((maximum - minimum) / 2, 0.000001)
        return max(0.6, 1 - (abs(value - midpoint) / half_width) * 0.4)
    if value < minimum:
        distance = minimum - value
    else:
        distance = value - maximum
    width = max(maximum - minimum, 0.000001)
    return max(0.0, 0.6 - (distance / width))


def _grid_hot_score(
    quote_volume: float,
    volatility_pct: float,
    change_pct: float,
    min_quote_volume: float,
    min_volatility_pct: float,
    max_volatility_pct: float,
    max_abs_change_pct: float,
) -> int:
    volume_score = min(30.0, (quote_volume / max(min_quote_volume, 1.0)) * 10.0)
    volatility_midpoint = (min_volatility_pct + max_volatility_pct) / 2
    volatility_width = max(max_volatility_pct - min_volatility_pct, 0.01)
    volatility_score = max(0.0, 40.0 - (abs(volatility_pct - volatility_midpoint) / volatility_width) * 45.0)
    move_score = max(0.0, 30.0 - (change_pct / max(max_abs_change_pct, 1.0)) * 30.0)
    return int(round(max(0.0, min(100.0, volume_score + volatility_score + move_score))))


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _is_plain_symbol(text: str) -> bool:
    return text.isascii() and text.replace("_", "").replace("-", "").isalnum()


def _to_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
