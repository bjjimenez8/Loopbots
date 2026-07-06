from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class OpportunityPaperConfig:
    state_file: str
    investment_usd: float = 1000.0
    starting_balance_usd: float = 10000.0
    fee_pct: float = 0.40


class OpportunityPaperTracker:
    def __init__(self, config: OpportunityPaperConfig) -> None:
        self.config = config
        self._path = Path(config.state_file)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def add_from_opportunity(self, opportunity: dict[str, Any]) -> dict[str, Any]:
        state = self._load_state()
        now = datetime.now(UTC).isoformat()
        existing = _existing_trade(state, opportunity, now)
        if existing is not None:
            return existing
        trade = _trade_from_opportunity(opportunity, now, self.config.investment_usd, self.config.fee_pct)
        state.append(trade)
        self._save_state(state)
        return trade

    def snapshot(self, candles_provider: Any, refresh: bool = True) -> dict[str, Any]:
        state = self._load_state()
        changed = False
        open_trades: list[dict[str, Any]] = []
        closed_trades: list[dict[str, Any]] = []
        for trade in state:
            if refresh and trade.get("status") == "OPEN":
                try:
                    candles = candles_provider(trade)
                    updated = _update_trade(trade, candles)
                except Exception as exc:
                    updated = {
                        **trade,
                        "last_checked_at": datetime.now(UTC).isoformat(),
                        "paper_note": f"Could not refresh market data yet: {exc}",
                    }
                changed = _merge_trade(trade, updated) or changed
            if trade.get("status") == "OPEN":
                open_trades.append(trade)
            else:
                closed_trades.append(trade)
        if changed:
            self._save_state(state)
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "investment_usd": self.config.investment_usd,
            "starting_balance_usd": self.config.starting_balance_usd,
            "fee_pct": self.config.fee_pct,
            "open": open_trades,
            "closed": closed_trades[-25:],
            "stats": _stats(closed_trades, open_trades, self.config.investment_usd, self.config.starting_balance_usd),
        }

    def _load_state(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return []
        return raw if isinstance(raw, list) else []

    def _save_state(self, state: list[dict[str, Any]]) -> None:
        self._path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _trade_from_opportunity(opportunity: dict[str, Any], now: str, investment_usd: float, fee_pct: float) -> dict[str, Any]:
    fields = opportunity.get("bitsgap_fields", {})
    if not isinstance(fields, dict):
        fields = {}
    strategy = str(opportunity.get("strategy", "")).upper()
    entry_price = _entry_price(opportunity, fields)
    take_profit_price = _take_profit_price(strategy, entry_price, fields)
    stop_price = _stop_price(strategy, entry_price, fields)
    return {
        "id": uuid.uuid4().hex[:12],
        "opportunity_id": str(opportunity.get("id", "")),
        "status": "OPEN",
        "strategy": strategy,
        "pair": str(opportunity.get("pair", "")),
        "entry_zone": str(opportunity.get("entry_zone", "")),
        "entry_price": entry_price,
        "take_profit_price": take_profit_price,
        "stop_price": stop_price,
        "investment_usd": investment_usd,
        "fee_pct": fee_pct,
        "settings": fields,
        "entry_reason": str(opportunity.get("reason", "")),
        "opened_at": now,
        "last_checked_at": now,
        "exit_price": None,
        "exit_reason": None,
        "closed_at": None,
        "gross_return_pct": None,
        "net_return_pct": None,
        "net_pnl_usd": None,
        "current_price": entry_price,
        "recent_closes": [],
        "recent_change_pct": None,
        "unrealized_net_return_pct": None,
        "unrealized_pnl_usd": None,
    }


def _update_trade(trade: dict[str, Any], candles: pd.DataFrame) -> dict[str, Any]:
    if candles.empty:
        return {**trade, "last_checked_at": datetime.now(UTC).isoformat(), "paper_note": "Waiting for fresh candles."}
    entry = _float(trade.get("entry_price"))
    tp = _float(trade.get("take_profit_price"))
    stop = _float(trade.get("stop_price"))
    if entry is None or entry <= 0:
        return {**trade, "last_checked_at": datetime.now(UTC).isoformat(), "paper_note": "Missing entry price."}

    recent_closes = [round(float(value), 10) for value in candles["close"].tail(24).tolist()]
    current = float(candles["close"].iloc[-1])
    recent_change = None
    if len(recent_closes) >= 2 and recent_closes[0] > 0:
        recent_change = round(((recent_closes[-1] / recent_closes[0]) - 1) * 100, 2)
    rows = _active_window(trade, candles)
    for _, candle in rows.iterrows():
        low = float(candle["low"])
        high = float(candle["high"])
        candle_time = candle["timestamp"].isoformat()
        event = None
        exit_price = None
        reason = None
        if stop and low <= stop and tp and high >= tp:
            event, exit_price, reason = "SAFETY_EXIT", stop, "TP and protection touched in same candle; counted protection first."
        elif stop and low <= stop:
            event, exit_price, reason = "SAFETY_EXIT", stop, "Protection level touched."
        elif tp and high >= tp:
            event, exit_price, reason = "TAKE_PROFIT", tp, "Take profit touched."
        if event and exit_price:
            return {
                **_close_trade(trade, exit_price, reason or event, candle_time),
                "recent_closes": recent_closes,
                "recent_change_pct": recent_change,
            }

    unrealized = ((current / entry) - 1) * 100 - float(trade.get("fee_pct", 0.0) or 0.0)
    investment = float(trade.get("investment_usd", 1000.0) or 1000.0)
    return {
        **trade,
        "current_price": current,
        "recent_closes": recent_closes,
        "recent_change_pct": recent_change,
        "unrealized_net_return_pct": round(unrealized, 2),
        "unrealized_pnl_usd": round(investment * unrealized / 100, 2),
        "last_checked_at": datetime.now(UTC).isoformat(),
        "paper_note": "Open. Watching TP and protection from Kraken candles.",
    }


def _close_trade(trade: dict[str, Any], exit_price: float, reason: str, closed_at: str) -> dict[str, Any]:
    entry = float(trade.get("entry_price", 0.0) or 0.0)
    fee = float(trade.get("fee_pct", 0.0) or 0.0)
    investment = float(trade.get("investment_usd", 1000.0) or 1000.0)
    gross = ((exit_price / entry) - 1) * 100 if entry > 0 else 0.0
    net = gross - fee
    return {
        **trade,
        "status": "CLOSED",
        "exit_price": exit_price,
        "exit_reason": reason,
        "closed_at": closed_at,
        "gross_return_pct": round(gross, 2),
        "net_return_pct": round(net, 2),
        "net_pnl_usd": round(investment * net / 100, 2),
        "current_price": exit_price,
        "unrealized_net_return_pct": None,
        "unrealized_pnl_usd": None,
        "last_checked_at": datetime.now(UTC).isoformat(),
    }


def _active_window(trade: dict[str, Any], candles: pd.DataFrame) -> pd.DataFrame:
    opened_at = trade.get("opened_at")
    if not opened_at:
        return candles
    try:
        opened = pd.Timestamp(str(opened_at))
    except ValueError:
        return candles
    if opened.tzinfo is None:
        opened = opened.tz_localize("UTC")
    opened = opened.tz_convert("UTC")
    return candles[candles["timestamp"] >= opened]


def _existing_trade(state: list[dict[str, Any]], opportunity: dict[str, Any], now: str) -> dict[str, Any] | None:
    today = now[:10]
    opportunity_id = str(opportunity.get("id", ""))
    coin_key = _coin_key(opportunity.get("pair", ""))

    for trade in reversed(state):
        if trade.get("status") != "OPEN":
            continue
        if opportunity_id and trade.get("opportunity_id") == opportunity_id:
            return trade
        if coin_key and _coin_key(trade.get("pair", "")) == coin_key:
            return trade

    for trade in reversed(state):
        opened_at = str(trade.get("opened_at", ""))
        if not opened_at.startswith(today):
            continue
        if opportunity_id and trade.get("opportunity_id") == opportunity_id:
            return trade
        if coin_key and _coin_key(trade.get("pair", "")) == coin_key:
            return trade
    return None


def _entry_price(opportunity: dict[str, Any], fields: dict[str, Any]) -> float | None:
    market = opportunity.get("market_snapshot", {})
    if isinstance(market, dict):
        current = _float(market.get("current_price"))
        if current and current > 0:
            return current
    low = _price(fields.get("Low price"))
    high = _price(fields.get("High price"))
    if low and high:
        return (low + high) / 2
    values = _numbers(str(opportunity.get("entry_zone", "")))
    if len(values) >= 2:
        return (values[0] + values[1]) / 2
    return values[0] if values else None


def _take_profit_price(strategy: str, entry: float | None, fields: dict[str, Any]) -> float | None:
    direct = _price(fields.get("Take profit"))
    if strategy == "LOOP":
        return direct
    pct = _percent(fields.get("Take profit"))
    if entry is not None and pct is not None:
        return entry * (1 + pct / 100)
    return direct


def _stop_price(strategy: str, entry: float | None, fields: dict[str, Any]) -> float | None:
    if strategy == "LOOP":
        return _price(fields.get("Stop loss")) or _price(fields.get("Safety exit / stop guidance"))
    pct = _percent(fields.get("Stop loss"))
    if entry is not None and pct is not None:
        return entry * (1 - pct / 100)
    return _price(fields.get("Stop loss"))


def _merge_trade(trade: dict[str, Any], updated: dict[str, Any]) -> bool:
    changed = False
    for key, value in updated.items():
        if trade.get(key) != value:
            trade[key] = value
            changed = True
    return changed


def _stats(
    closed: list[dict[str, Any]],
    open_trades: list[dict[str, Any]],
    investment_usd: float,
    starting_balance_usd: float,
) -> dict[str, Any]:
    wins = [row for row in closed if float(row.get("net_return_pct", 0.0) or 0.0) > 0]
    total_pnl = sum(float(row.get("net_pnl_usd", 0.0) or 0.0) for row in closed)
    open_pnl = sum(float(row.get("unrealized_pnl_usd", 0.0) or 0.0) for row in open_trades)
    return {
        "open": len(open_trades),
        "closed": len(closed),
        "wins": len(wins),
        "losses": max(len(closed) - len(wins), 0),
        "win_rate_pct": round((len(wins) / len(closed)) * 100, 2) if closed else 0.0,
        "net_pnl_usd": round(total_pnl, 2),
        "open_pnl_usd": round(open_pnl, 2),
        "equity_pnl_usd": round(total_pnl + open_pnl, 2),
        "paper_balance_usd": round(starting_balance_usd + total_pnl + open_pnl, 2),
        "investment_per_trade": investment_usd,
        "starting_balance_usd": starting_balance_usd,
    }


def _price(value: Any) -> float | None:
    values = _numbers(str(value))
    return values[0] if values else None


def _percent(value: Any) -> float | None:
    values = _numbers(str(value))
    return values[0] if values else None


def _numbers(text: str) -> list[float]:
    return [float(item.replace(",", "")) for item in re.findall(r"\d[\d,]*(?:\.\d+)?", text)]


def _float(value: Any) -> float | None:
    try:
        if value in {"", None}:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _coin_key(pair: Any) -> str:
    text = str(pair or "").strip().upper()
    if not text:
        return ""
    return text.split("/", 1)[0].strip()
