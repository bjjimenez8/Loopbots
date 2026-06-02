from __future__ import annotations

import csv
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PaperTrackingConfig:
    enabled: bool
    lookback_days: int
    retention_days: int
    fee_pct: float


class PaperTracker:
    def __init__(
        self,
        active_trades_file: str,
        trade_history_file: str,
        config: PaperTrackingConfig,
    ) -> None:
        self.active_trades_path = Path(active_trades_file)
        self.trade_history_path = Path(trade_history_file)
        self.config = config

    def build_summary(self) -> str:
        snapshot = self.snapshot()
        window_stats = snapshot["window_stats"]
        all_stats = snapshot["all_stats"]
        active_trades = snapshot["active_trades"]

        lines = [
            "📊 LOOPBOTS PAPER CHECK",
            f"Window: last {self.config.lookback_days} days",
            (
                f"Closed: {window_stats['closed']} | Wins: {window_stats['wins']} | "
                f"Losses: {window_stats['losses']} | WR: {window_stats['win_rate_pct']:.2f}%"
            ),
            f"Net after est. fees: {window_stats['net_return_pct']:+.2f}%",
            f"Avg net/trade: {window_stats['avg_net_return_pct']:+.2f}%",
            f"Avg hold: {window_stats['avg_hold_hours']:.2f}h",
            f"Active alerts: {len(active_trades)}",
        ]

        if window_stats["best_symbol"]:
            lines.append(f"Best: {window_stats['best_symbol']} {window_stats['best_symbol_return_pct']:+.2f}%")
        if window_stats["worst_symbol"]:
            lines.append(f"Worst: {window_stats['worst_symbol']} {window_stats['worst_symbol_return_pct']:+.2f}%")

        lines.extend(
            [
                "",
                "All-time paper:",
                (
                    f"Closed: {all_stats['closed']} | WR: {all_stats['win_rate_pct']:.2f}% | "
                    f"Net: {all_stats['net_return_pct']:+.2f}% | "
                    f"Avg/trade: {all_stats['avg_net_return_pct']:+.2f}%"
                ),
            ]
        )

        if active_trades:
            lines.extend(["", "Open paper alerts:"])
            for trade in active_trades:
                lines.append(
                    f"- {trade.get('symbol')} {trade.get('preset')} | Entry {trade.get('entry_price')} | "
                    f"TP {trade.get('take_profit_price')} | Stop {trade.get('safety_exit_price')}"
                )

        return "\n".join(lines)

    def snapshot(self) -> dict[str, Any]:
        rows = self._read_history()
        active_trades = self._active_trade_records(self._read_active_trades())
        closed_rows = [
            row
            for row in rows
            if row.get("event") in {"TAKE_PROFIT", "EXIT"} and self._is_complete_closed_row(row)
        ]
        window_start = datetime.now(UTC) - timedelta(days=self.config.lookback_days)
        window_rows = [
            row
            for row in closed_rows
            if (event_at := self._parse_datetime(row.get("event_at", ""))) is not None and event_at >= window_start
        ]

        closed_trades = [self._closed_trade_record(row) for row in closed_rows]
        closed_trades.sort(key=lambda row: row.get("event_at", ""), reverse=True)

        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "lookback_days": self.config.lookback_days,
            "retention_days": self.config.retention_days,
            "fee_pct": self.config.fee_pct,
            "window_stats": self._stats(window_rows),
            "all_stats": self._stats(closed_rows),
            "active_trades": active_trades,
            "closed_trades": closed_trades,
        }

    def _read_history(self) -> list[dict[str, str]]:
        if not self.trade_history_path.exists():
            return []
        with self.trade_history_path.open("r", newline="", encoding="utf-8") as file:
            return list(csv.DictReader(file))

    def _read_active_trades(self) -> dict[str, dict[str, Any]]:
        if not self.active_trades_path.exists():
            return {}
        with self.active_trades_path.open("r", encoding="utf-8") as file:
            return json.load(file)

    def _stats(self, rows: list[dict[str, str]]) -> dict[str, Any]:
        closed = len(rows)
        wins = sum(1 for row in rows if row.get("event") == "TAKE_PROFIT")
        losses = sum(1 for row in rows if row.get("event") == "EXIT")
        win_rate_pct = (wins / closed * 100) if closed else 0.0
        net_returns = [self._net_return_pct(row) for row in rows]
        hold_hours = [self._hold_hours(row) for row in rows]
        symbol_returns: dict[str, float] = defaultdict(float)
        for row, net_return in zip(rows, net_returns, strict=False):
            symbol_returns[row.get("symbol", "")] += net_return

        best_symbol = ""
        worst_symbol = ""
        if symbol_returns:
            best_symbol = max(symbol_returns, key=symbol_returns.get)
            worst_symbol = min(symbol_returns, key=symbol_returns.get)

        return {
            "closed": closed,
            "wins": wins,
            "losses": losses,
            "win_rate_pct": win_rate_pct,
            "net_return_pct": sum(net_returns),
            "avg_net_return_pct": (sum(net_returns) / closed) if closed else 0.0,
            "avg_hold_hours": (sum(hold_hours) / len(hold_hours)) if hold_hours else 0.0,
            "best_symbol": best_symbol,
            "best_symbol_return_pct": symbol_returns.get(best_symbol, 0.0),
            "worst_symbol": worst_symbol,
            "worst_symbol_return_pct": symbol_returns.get(worst_symbol, 0.0),
        }

    def _active_trade_records(self, active_trades: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        records = []
        for symbol, trade in sorted(active_trades.items()):
            records.append(
                {
                    "symbol": symbol,
                    "preset": self._preset_name(trade),
                    "entry_price": self._to_float(trade.get("entry_price")),
                    "take_profit_price": self._to_float(trade.get("take_profit_price")),
                    "safety_exit_price": self._to_float(trade.get("safety_exit_price")),
                    "opened_at": trade.get("opened_at", ""),
                    "reason": trade.get("reason", ""),
                }
            )
        return records

    def _closed_trade_record(self, row: dict[str, str]) -> dict[str, Any]:
        return {
            "event": row.get("event", ""),
            "symbol": row.get("symbol", ""),
            "preset": self._preset_name_from_history(row),
            "entry_price": self._to_float(row.get("entry_price")),
            "exit_price": self._to_float(row.get("exit_price")),
            "take_profit_price": self._to_float(row.get("take_profit_price")),
            "safety_exit_price": self._to_float(row.get("safety_exit_price")),
            "net_return_pct": self._net_return_pct(row),
            "grid_cycles": int(self._to_float(row.get("grid_cycles"))),
            "grid_net_return_pct": self._to_float(row.get("grid_net_return_pct")),
            "price_net_return_pct": self._to_float(row.get("price_net_return_pct")),
            "hold_hours": self._hold_hours(row),
            "opened_at": row.get("opened_at", ""),
            "event_at": row.get("event_at", ""),
            "reason": row.get("reason", ""),
            "exit_reason": row.get("exit_reason", ""),
        }

    def _net_return_pct(self, row: dict[str, str]) -> float:
        total_net = self._to_float(row.get("total_net_return_pct"))
        if total_net:
            return total_net
        entry = self._to_float(row.get("entry_price"))
        exit_price = self._to_float(row.get("exit_price"))
        if entry <= 0 or exit_price <= 0:
            return 0.0
        return ((exit_price / entry) - 1) * 100 - self.config.fee_pct

    def _hold_hours(self, row: dict[str, str]) -> float:
        opened_at = self._parse_datetime(row.get("opened_at", ""))
        event_at = self._parse_datetime(row.get("event_at", ""))
        if opened_at is None or event_at is None:
            return 0.0
        return max((event_at - opened_at).total_seconds() / 3600, 0.0)

    @staticmethod
    def _is_complete_closed_row(row: dict[str, str]) -> bool:
        return bool(row.get("entry_price") and row.get("exit_price"))

    @staticmethod
    def _parse_datetime(value: str) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    @staticmethod
    def _preset_name(trade: dict[str, Any]) -> str:
        loop_settings = trade.get("loop_settings") or {}
        loop_plan = loop_settings.get("loop_plan") or {}
        return str(loop_plan.get("preset_name") or loop_settings.get("preset_name") or "Mid-term")

    @staticmethod
    def _preset_name_from_history(row: dict[str, str]) -> str:
        try:
            loop_settings = json.loads(row.get("loop_settings") or "{}")
        except json.JSONDecodeError:
            loop_settings = {}
        loop_plan = loop_settings.get("loop_plan") or {}
        return str(loop_plan.get("preset_name") or loop_settings.get("preset_name") or "Mid-term")

    @staticmethod
    def _to_float(value: str | None) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0
