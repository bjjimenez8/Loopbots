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
    history_file: str
    setups: list[GridSetup]


class GridWatchService:
    def __init__(self, config: GridWatchConfig) -> None:
        self.config = config
        if not hasattr(ccxt, config.exchange_id):
            raise ValueError(f"Unsupported GRID exchange id: {config.exchange_id}")
        exchange_class = getattr(ccxt, config.exchange_id)
        self.exchange = exchange_class({"enableRateLimit": True})
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
                score=int(item["score"]),
            )
            for item in raw_config.get("setups", [])
        ]
        state_file = str(project_root / raw_config.get("state_file", "data/grid_watch_state.json"))
        history_file = str(project_root / raw_config.get("history_file", "data/grid_trade_history.csv"))
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
                history_file=history_file,
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
            state_item = state.get(key)
            if self._has_active_grid(state_item) or self._in_cooldown(state_item):
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
            now = datetime.now(UTC).isoformat()
            state[key] = {
                "last_alert_at": now,
                "symbol": setup.symbol,
                "preset_name": setup.preset_name,
                "active_grid": self._build_active_record(alert, now),
            }
            self._append_history(self._build_entry_history_row(alert, now))

        if alerts:
            self._save_state(state)
        return alerts

    def find_exit_alerts(self) -> list[dict[str, Any]]:
        if not self.config.enabled:
            return []

        state = self._load_state()
        exit_alerts: list[dict[str, Any]] = []
        changed = False
        for setup in self.config.setups:
            key = self._state_key(setup)
            state_item = state.get(key)
            if not isinstance(state_item, dict):
                continue

            active_grid = state_item.get("active_grid")
            if not isinstance(active_grid, dict):
                continue

            try:
                candles = self._fetch_candles(setup.symbol)
            except Exception:
                LOGGER.exception("Failed to fetch GRID exit candles for %s", setup.symbol)
                continue

            current_price = float(candles["close"].iloc[-1])
            take_profit_price = float(active_grid.get("take_profit_price") or 0)
            stop_loss_price = float(active_grid.get("stop_loss_price") or 0)
            exit_reason = ""
            if take_profit_price > 0 and current_price >= take_profit_price:
                exit_reason = "Take Profit"
            elif stop_loss_price > 0 and current_price <= stop_loss_price:
                exit_reason = "Stop Loss"

            if not exit_reason:
                continue

            exit_alert = self._build_exit_alert(setup, active_grid, current_price, exit_reason)
            exit_alerts.append(exit_alert)
            self._append_history(self._build_exit_history_row(active_grid, exit_alert))
            state_item.pop("active_grid", None)
            state_item["last_exit_at"] = datetime.now(UTC).isoformat()
            state_item["last_exit_reason"] = exit_reason
            state[key] = state_item
            changed = True

        if changed:
            self._save_state(state)
        return exit_alerts

    def diagnostics(self) -> list[dict[str, Any]]:
        if not self.config.enabled:
            return []

        state = self._load_state()
        results: list[dict[str, Any]] = []
        for setup in self.config.setups:
            key = self._state_key(setup)
            state_item = state.get(key)
            active = self._has_active_grid(state_item)
            cooldown = self._in_cooldown(state_item)
            try:
                candles = self._fetch_candles(setup.symbol)
                status = self._sideways_status(candles)
            except Exception as exc:
                LOGGER.exception("Failed to build GRID diagnostics for %s", setup.symbol)
                results.append(
                    {
                        "symbol": setup.symbol,
                        "ready": False,
                        "active": active,
                        "cooldown": cooldown,
                        "reason": f"data error: {exc}",
                    }
                )
                continue

            reasons = []
            if active:
                reasons.append("active")
            if cooldown:
                reasons.append("cooldown")
            reasons.extend(status["reasons"])
            ready = not active and not cooldown and status["passes"]
            results.append(
                {
                    "symbol": setup.symbol,
                    "ready": ready,
                    "active": active,
                    "cooldown": cooldown,
                    "reason": "READY" if ready else "; ".join(reasons),
                    **status,
                }
            )
        return results

    def paper_snapshot(self) -> dict[str, Any]:
        rows = self._read_history()
        closed_rows = [row for row in rows if row.get("event") in {"GRID_TAKE_PROFIT", "GRID_STOP_LOSS"}]
        active_count = sum(1 for row in rows if row.get("event") == "GRID_ENTRY") - len(closed_rows)
        wins = sum(1 for row in closed_rows if row.get("event") == "GRID_TAKE_PROFIT")
        losses = sum(1 for row in closed_rows if row.get("event") == "GRID_STOP_LOSS")
        net_returns = [_to_float(row.get("net_return_pct")) for row in closed_rows]
        return {
            "entries": sum(1 for row in rows if row.get("event") == "GRID_ENTRY"),
            "closed": len(closed_rows),
            "active": max(active_count, 0),
            "wins": wins,
            "losses": losses,
            "win_rate_pct": (wins / len(closed_rows) * 100) if closed_rows else 0.0,
            "net_return_pct": sum(net_returns),
            "avg_net_return_pct": (sum(net_returns) / len(closed_rows)) if closed_rows else 0.0,
        }

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
        return bool(self._sideways_status(candles)["passes"])

    def _sideways_status(self, candles: pd.DataFrame) -> dict[str, Any]:
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
        reasons = []
        if not -5 <= trend_return_pct <= 8:
            reasons.append(f"trend {trend_return_pct:.2f}%")
        if not 5 <= range_pct <= 25:
            reasons.append(f"range {range_pct:.2f}%")
        if directional_efficiency > 0.40:
            reasons.append(f"directional {directional_efficiency:.2f}")
        if not 0.20 <= range_position <= 0.80:
            reasons.append(f"position {range_position:.2f}")
        return {
            "passes": not reasons,
            "reasons": reasons,
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
        grid_step_pct = (setup.lower_pct + setup.upper_pct) / setup.levels
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
            "stop_loss_price": stop_loss_price,
            "take_profit_price": take_profit_price,
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

    @staticmethod
    def _build_active_record(alert: dict[str, Any], started_at: str) -> dict[str, Any]:
        return {
            "symbol": alert["symbol"],
            "preset_name": alert["preset_name"],
            "started_at": started_at,
            "entry_price": alert["entry_price"],
            "low_price": alert["low_price"],
            "high_price": alert["high_price"],
            "stop_loss_price": alert["stop_loss_price"],
            "take_profit_price": alert["take_profit_price"],
        }

    def _build_exit_alert(
        self,
        setup: GridSetup,
        active_grid: dict[str, Any],
        current_price: float,
        exit_reason: str,
    ) -> dict[str, Any]:
        entry_price = float(active_grid.get("entry_price") or 0.0)
        gross_return_pct = ((current_price / entry_price) - 1) * 100 if entry_price > 0 else 0.0
        return {
            "symbol": setup.symbol,
            "exchange": "Kraken",
            "current_price": _fmt_price(current_price),
            "exit_reason": exit_reason,
            "entry_price": _fmt_price(entry_price),
            "gross_return_pct": round(gross_return_pct, 4),
            "net_return_pct": round(gross_return_pct, 4),
            "opened_at": str(active_grid.get("started_at", "")),
            "event_at": datetime.now(UTC).isoformat(),
        }

    @staticmethod
    def _has_active_grid(state_item: Any) -> bool:
        return isinstance(state_item, dict) and isinstance(state_item.get("active_grid"), dict)

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
            return
        with self._history_path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=self._history_fields)
            writer.writeheader()

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
            "grid_step_pct": alert.get("grid_step_pct", ""),
            "levels": alert.get("levels", ""),
            "take_profit_pct": alert.get("take_profit_pct", ""),
            "stop_loss_pct": alert.get("stop_loss_pct", ""),
            "opened_at": event_at,
        }

    @staticmethod
    def _build_exit_history_row(active_grid: dict[str, Any], exit_alert: dict[str, Any]) -> dict[str, Any]:
        event = "GRID_TAKE_PROFIT" if exit_alert.get("exit_reason") == "Take Profit" else "GRID_STOP_LOSS"
        return {
            "event": event,
            "event_at": exit_alert.get("event_at", ""),
            "symbol": active_grid.get("symbol", ""),
            "preset_name": active_grid.get("preset_name", ""),
            "exchange": exit_alert.get("exchange", ""),
            "entry_price": active_grid.get("entry_price", ""),
            "exit_price": exit_alert.get("current_price", ""),
            "low_price": active_grid.get("low_price", ""),
            "high_price": active_grid.get("high_price", ""),
            "gross_return_pct": exit_alert.get("gross_return_pct", ""),
            "net_return_pct": exit_alert.get("net_return_pct", ""),
            "opened_at": active_grid.get("started_at", ""),
            "exit_reason": exit_alert.get("exit_reason", ""),
        }


def _fmt_number(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _fmt_price(value: float) -> str:
    if value >= 100:
        return f"{value:.2f}"
    if value >= 1:
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return f"{value:.8f}".rstrip("0").rstrip(".")


def _to_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
