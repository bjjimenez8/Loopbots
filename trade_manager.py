from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from strategy import Signal


class TradeManager:
    def __init__(self, active_trades_file: str, trade_history_file: str) -> None:
        self.active_trades_path = Path(active_trades_file)
        self.trade_history_path = Path(trade_history_file)
        self.active_trades_path.parent.mkdir(parents=True, exist_ok=True)
        self.trade_history_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_files()

    def load_active_trades(self) -> dict[str, dict[str, Any]]:
        with self.active_trades_path.open("r", encoding="utf-8") as file:
            return json.load(file)

    def has_active_trade(self, symbol: str) -> bool:
        return symbol in self.load_active_trades()

    def get_active_trade(self, symbol: str) -> dict[str, Any] | None:
        return self.load_active_trades().get(symbol)

    def open_trade(self, signal: Signal) -> dict[str, Any]:
        active_trades = self.load_active_trades()
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
        }
        self._save_active_trades(active_trades)
        self._append_history(closed_trade)
        return closed_trade

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
        ]

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()
