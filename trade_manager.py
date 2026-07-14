from __future__ import annotations

import csv
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from strategy import Signal


class TradeManager:
    def __init__(self, active_trades_file: str, trade_history_file: str, fee_pct: float = 0.2) -> None:
        self.active_trades_path = Path(active_trades_file)
        self.trade_history_path = Path(trade_history_file)
        self.fee_pct = fee_pct
        self.active_trades_path.parent.mkdir(parents=True, exist_ok=True)
        self.trade_history_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_files()

    def load_active_trades(self) -> dict[str, dict[str, Any]]:
        self._ensure_files()
        try:
            with self.active_trades_path.open("r", encoding="utf-8") as file:
                active_trades = json.load(file)
        except json.JSONDecodeError:
            active_trades = {}
            self._save_active_trades(active_trades)

        if not isinstance(active_trades, dict):
            active_trades = {}
            self._save_active_trades(active_trades)
        return active_trades

    def has_active_trade(self, symbol: str) -> bool:
        return symbol in self.load_active_trades()

    def get_active_trade(self, symbol: str) -> dict[str, Any] | None:
        return self.load_active_trades().get(symbol)

    def open_trade(self, signal: Signal) -> dict[str, Any] | None:
        active_trades = self.load_active_trades()
        if signal.symbol in active_trades:
            return None

        now = self._now()
        trade = {
            "symbol": signal.symbol,
            "entry_price": signal.price,
            "take_profit_price": signal.take_profit_price,
            "safety_exit_price": signal.safety_exit_price,
            "opened_at": now,
            "status": "ACTIVE",
            "reason": signal.reason,
            "loop_settings": signal.loop_settings or {},
            "paper_grid": self._new_grid_state(signal),
        }
        active_trades[signal.symbol] = trade
        self._save_active_trades(active_trades)
        self._append_history({**trade, "event": "ENTER", "event_at": now, "exit_price": ""})
        return trade

    def close_trade(self, signal: Signal, exit_reason: str, event: str = "EXIT") -> dict[str, Any] | None:
        active_trades = self.load_active_trades()
        trade = active_trades.pop(signal.symbol, None)
        if trade is None:
            return None

        now = self._now()
        closed_trade = {
            **trade,
            "event": event,
            "event_at": now,
            "exit_price": signal.price,
            "exit_reason": exit_reason,
            "status": "CLOSED",
            **self._paper_returns(trade, signal.price),
        }
        self._save_active_trades(active_trades)
        self._append_history(closed_trade)
        return closed_trade

    def update_paper_grid(self, symbol: str, candle: Any) -> dict[str, Any] | None:
        active_trades = self.load_active_trades()
        trade = active_trades.get(symbol)
        if trade is None:
            return None

        timestamp = str(candle.get("timestamp", ""))
        paper_grid = trade.get("paper_grid") or self._new_grid_state_from_trade(trade)
        if timestamp and paper_grid.get("last_timestamp") == timestamp:
            return trade

        self._update_grid_state(paper_grid, candle)
        paper_grid["last_timestamp"] = timestamp
        trade["paper_grid"] = paper_grid
        active_trades[symbol] = trade
        self._save_active_trades(active_trades)
        return trade

    def current_total_net_return_pct(self, trade: dict[str, Any], current_price: float) -> float:
        return float(self._paper_returns(trade, current_price)["total_net_return_pct"])

    def prune_history(self, retention_days: int) -> int:
        if retention_days <= 0 or not self.trade_history_path.exists():
            return 0

        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        active_symbols = set(self.load_active_trades())
        with self.trade_history_path.open("r", newline="", encoding="utf-8") as file:
            rows = list(csv.DictReader(file))

        kept_rows = []
        for row in rows:
            if row.get("event") == "ENTER" and row.get("symbol") in active_symbols:
                kept_rows.append(row)
                continue

            event_time = self._parse_time(row.get("event_at", "") or row.get("opened_at", ""))
            if event_time is None or event_time >= cutoff:
                kept_rows.append(row)

        removed_count = len(rows) - len(kept_rows)
        if removed_count:
            with self.trade_history_path.open("w", newline="", encoding="utf-8") as file:
                writer = csv.DictWriter(file, fieldnames=self._history_fields)
                writer.writeheader()
                writer.writerows({field: row.get(field, "") for field in self._history_fields} for row in kept_rows)
        return removed_count

    def _ensure_files(self) -> None:
        if not self.active_trades_path.exists():
            self.active_trades_path.write_text("{}", encoding="utf-8")

        if not self.trade_history_path.exists():
            with self.trade_history_path.open("w", newline="", encoding="utf-8") as file:
                writer = csv.DictWriter(file, fieldnames=self._history_fields)
                writer.writeheader()

    def _save_active_trades(self, active_trades: dict[str, dict[str, Any]]) -> None:
        self.active_trades_path.write_text(
            json.dumps(active_trades, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def _append_history(self, row: dict[str, Any]) -> None:
        normalized = {field: row.get(field, "") for field in self._history_fields}
        normalized["loop_settings"] = json.dumps(normalized["loop_settings"])
        with self.trade_history_path.open("a", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=self._history_fields)
            writer.writerow(normalized)

    @property
    def _history_fields(self) -> list[str]:
        return [
            "event",
            "event_at",
            "symbol",
            "status",
            "entry_price",
            "exit_price",
            "take_profit_price",
            "safety_exit_price",
            "opened_at",
            "reason",
            "exit_reason",
            "loop_settings",
            "grid_cycles",
            "grid_gross_return_pct",
            "grid_net_return_pct",
            "price_gross_return_pct",
            "price_net_return_pct",
            "total_gross_return_pct",
            "total_net_return_pct",
        ]

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    def _new_grid_state(self, signal: Signal) -> dict[str, Any]:
        loop_settings = signal.loop_settings or {}
        return self._grid_state(
            entry_price=float(signal.price),
            loop_settings=loop_settings,
        )

    def _new_grid_state_from_trade(self, trade: dict[str, Any]) -> dict[str, Any]:
        return self._grid_state(
            entry_price=float(trade.get("entry_price") or 0.0),
            loop_settings=trade.get("loop_settings") or {},
        )

    def _grid_state(self, entry_price: float, loop_settings: dict[str, Any]) -> dict[str, Any]:
        loop_plan = loop_settings.get("loop_plan") or {}
        order_distance_pct = float(
            loop_plan.get("order_distance_pct")
            or loop_settings.get("order_distance_pct")
            or 1.5
        )
        order_count = int(loop_plan.get("order_count") or loop_settings.get("order_count") or 10)
        return {
            "entry_price": entry_price,
            "order_distance_pct": order_distance_pct,
            "order_count": order_count,
            "step": order_distance_pct / 100,
            "fee_pct": self.fee_pct,
            "max_pending_orders": max(order_count // 2, 1),
            "pending_levels": [],
            "cycles": 0,
            "gross_return_pct": 0.0,
            "net_return_pct": 0.0,
            "last_timestamp": "",
        }

    def _update_grid_state(self, grid_state: dict[str, Any], candle: Any) -> None:
        for price in self._candle_path(candle):
            self._process_grid_price(grid_state, price)

    @staticmethod
    def _candle_path(candle: Any) -> list[float]:
        open_price = float(candle["open"])
        high_price = float(candle["high"])
        low_price = float(candle["low"])
        close_price = float(candle["close"])
        if close_price >= open_price:
            return [open_price, low_price, high_price, close_price]
        return [open_price, high_price, low_price, close_price]

    @staticmethod
    def _process_grid_price(grid_state: dict[str, Any], price: float) -> None:
        entry_price = float(grid_state["entry_price"])
        step = float(grid_state["step"])
        max_pending_orders = int(grid_state["max_pending_orders"])
        pending_levels = set(grid_state["pending_levels"])

        for level in range(1, max_pending_orders + 1):
            if level in pending_levels:
                continue
            buy_price = entry_price * ((1 - step) ** level)
            if price <= buy_price:
                pending_levels.add(level)

        for level in sorted(pending_levels):
            buy_price = entry_price * ((1 - step) ** level)
            sell_price = buy_price * (1 + step)
            if price >= sell_price:
                pending_levels.remove(level)
                grid_state["cycles"] += 1
                grid_state["gross_return_pct"] += grid_state["order_distance_pct"] / grid_state["order_count"]
                grid_state["net_return_pct"] += (
                    grid_state["order_distance_pct"] - grid_state["fee_pct"]
                ) / grid_state["order_count"]

        grid_state["pending_levels"] = sorted(pending_levels)

    def _paper_returns(self, trade: dict[str, Any], exit_price: float) -> dict[str, Any]:
        entry_price = float(trade.get("entry_price") or 0.0)
        paper_grid = trade.get("paper_grid") or self._new_grid_state_from_trade(trade)
        price_gross = ((exit_price / entry_price) - 1) * 100 if entry_price > 0 else 0.0
        price_net = price_gross - self.fee_pct
        grid_gross = float(paper_grid.get("gross_return_pct") or 0.0)
        grid_net = float(paper_grid.get("net_return_pct") or 0.0)
        return {
            "grid_cycles": int(paper_grid.get("cycles") or 0),
            "grid_gross_return_pct": round(grid_gross, 4),
            "grid_net_return_pct": round(grid_net, 4),
            "price_gross_return_pct": round(price_gross, 4),
            "price_net_return_pct": round(price_net, 4),
            "total_gross_return_pct": round(price_gross + grid_gross, 4),
            "total_net_return_pct": round(price_net + grid_net, 4),
        }

    @staticmethod
    def _parse_time(value: str) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
