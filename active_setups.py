from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import pandas as pd


RecommendedAction = Literal["HOLD", "TAKE_PROFIT", "MOVE_STOP_UP", "TRAIL_PROFIT", "EXIT"]


@dataclass(frozen=True)
class ActiveSetupConfig:
    state_file: str


class ActiveSetupStore:
    def __init__(self, config: ActiveSetupConfig) -> None:
        self.config = config
        self._path = Path(config.state_file)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def add_from_opportunity(self, opportunity: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        setup = _setup_from_opportunity(opportunity, now)
        state = self._load_state()
        state.append(setup)
        self._save_state(state)
        return setup

    def finish(self, setup_id: str) -> bool:
        state = self._load_state()
        changed = False
        now = datetime.now(UTC).isoformat()
        for setup in state:
            if str(setup.get("id")) == setup_id and setup.get("status") == "ACTIVE":
                setup["status"] = "FINISHED"
                setup["finished_at"] = now
                changed = True
        if changed:
            self._save_state(state)
        return changed

    def snapshot(self, candles_provider: Any) -> dict[str, Any]:
        state = self._load_state()
        changed = False
        active_rows: list[dict[str, Any]] = []
        finished_rows: list[dict[str, Any]] = []
        for setup in state:
            if setup.get("status") != "ACTIVE":
                finished_rows.append(setup)
                continue
            try:
                candles = candles_provider(setup)
                monitored = _monitor_setup(setup, candles)
            except Exception as exc:
                monitored = {
                    **setup,
                    "recommended_action": "HOLD",
                    "health": "Monitor",
                    "guidance": f"Could not refresh market data yet: {exc}",
                    "action_changed": False,
                    "checked_at": datetime.now(UTC).isoformat(),
                }
            changed = _merge_monitor_state(setup, monitored) or changed
            active_rows.append(monitored)
        if changed:
            self._save_state(state)
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "active": active_rows,
            "finished": finished_rows[-20:],
        }

    def _load_state(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return raw if isinstance(raw, list) else []

    def _save_state(self, state: list[dict[str, Any]]) -> None:
        self._path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _setup_from_opportunity(opportunity: dict[str, Any], now: str) -> dict[str, Any]:
    fields = opportunity.get("bitsgap_fields", {})
    if not isinstance(fields, dict):
        fields = {}
    strategy = str(opportunity.get("strategy", "")).upper()
    pair = str(opportunity.get("pair", ""))
    entry_price = _entry_price(opportunity, fields)
    take_profit = _price(fields.get("Take profit"))
    stop_price = _price(fields.get("Safety exit / stop guidance")) or _stop_from_pct(entry_price, fields.get("Stop loss"))
    if take_profit is None and entry_price is not None:
        take_profit = entry_price * (1 + (_percent(fields.get("Take profit")) or 0) / 100)
    return {
        "id": uuid.uuid4().hex[:12],
        "status": "ACTIVE",
        "strategy": strategy,
        "pair": pair,
        "entry_zone": str(opportunity.get("entry_zone", "")),
        "entry_price": entry_price,
        "take_profit_price": take_profit,
        "stop_price": stop_price,
        "original_stop_price": stop_price,
        "high_water_price": entry_price,
        "bitsgap_fields": fields,
        "reason": str(opportunity.get("reason", "")),
        "recommended_action": "HOLD",
        "last_recommended_action": "HOLD",
        "action_changed_at": now,
        "created_at": now,
        "updated_at": now,
    }


def _monitor_setup(setup: dict[str, Any], candles: pd.DataFrame) -> dict[str, Any]:
    if candles.empty:
        return {**setup, "recommended_action": "HOLD", "health": "Monitor", "guidance": "Waiting for fresh candles."}

    close = float(candles["close"].iloc[-1])
    recent = _active_window(setup, candles).tail(min(len(candles), 24))
    if recent.empty:
        recent_low = close
        recent_high = close
    else:
        recent_low = float(recent["low"].min())
        recent_high = float(recent["high"].max())
    entry = _float(setup.get("entry_price")) or close
    tp = _float(setup.get("take_profit_price"))
    stop = _float(setup.get("stop_price"))
    high_water = max(_float(setup.get("high_water_price")) or entry, recent_high, close)
    profit_pct = ((close / entry) - 1) * 100 if entry > 0 else 0.0
    drawdown_from_high_pct = ((close / high_water) - 1) * 100 if high_water > 0 else 0.0
    stop_distance_pct = ((close / stop) - 1) * 100 if stop and stop > 0 else 0.0
    tp_distance_pct = ((tp / close) - 1) * 100 if tp and close > 0 else 0.0
    strategy = str(setup.get("strategy", "")).upper()

    action: RecommendedAction = "HOLD"
    health = "Healthy"
    guidance = "Keep running."
    suggested_stop = stop

    if stop and recent_low <= stop:
        action, health = "EXIT", "Exit Suggested"
        guidance = "Safety exit / stop area was touched. Consider closing the manual setup."
    elif tp and close >= tp:
        action, health = "TAKE_PROFIT", "Take Profit Suggested"
        guidance = "Take profit area is reached. Lock the win or trail only if momentum continues."
    elif profit_pct >= 3.0:
        action, health = "TRAIL_PROFIT", "Profit Protection"
        guidance = "Price moved strongly in your favor. Trail profit and do not let it round-trip."
        suggested_stop = max(stop or entry, entry * 1.01, close * 0.975)
    elif profit_pct >= 1.25:
        action, health = "MOVE_STOP_UP", "Protect Profit"
        guidance = "Move safety exit / stop closer to breakeven to reduce give-back risk."
        suggested_stop = max(stop or entry, entry * 1.001)
    elif drawdown_from_high_pct <= -3.5 or (stop_distance_pct and stop_distance_pct <= 0.6):
        action, health = "EXIT", "Exit Suggested"
        guidance = "Setup is weakening near the protection line. Consider exiting before loss expands."
    elif strategy == "GRID" and drawdown_from_high_pct <= -2.0:
        action, health = "Monitor"
        guidance = "Grid is pulling back. Keep running only while price stays inside the planned range."

    return {
        **setup,
        "current_price": close,
        "high_water_price": high_water,
        "profit_pct": round(profit_pct, 2),
        "drawdown_from_high_pct": round(drawdown_from_high_pct, 2),
        "tp_distance_pct": round(tp_distance_pct, 2),
        "recommended_action": action,
        "last_recommended_action": action,
        "health": health,
        "guidance": guidance,
        "suggested_stop_price": suggested_stop,
        "action_changed": action != setup.get("last_recommended_action"),
        "checked_at": datetime.now(UTC).isoformat(),
    }


def _active_window(setup: dict[str, Any], candles: pd.DataFrame) -> pd.DataFrame:
    created_at = setup.get("created_at")
    if not created_at:
        return candles
    try:
        created = pd.Timestamp(str(created_at))
    except ValueError:
        return candles
    if created.tzinfo is None:
        created = created.tz_localize("UTC")
    created = created.tz_convert("UTC")
    return candles[candles["timestamp"] >= created]


def _merge_monitor_state(setup: dict[str, Any], monitored: dict[str, Any]) -> bool:
    changed = False
    for key in ["current_price", "high_water_price", "profit_pct", "drawdown_from_high_pct", "tp_distance_pct", "health", "guidance", "suggested_stop_price", "checked_at"]:
        if setup.get(key) != monitored.get(key):
            setup[key] = monitored.get(key)
            changed = True
    action = monitored.get("recommended_action", "HOLD")
    if setup.get("last_recommended_action") != action:
        setup["last_recommended_action"] = action
        setup["recommended_action"] = action
        setup["action_changed_at"] = datetime.now(UTC).isoformat()
        changed = True
    setup["updated_at"] = datetime.now(UTC).isoformat()
    return changed


def _entry_price(opportunity: dict[str, Any], fields: dict[str, Any]) -> float | None:
    low = _price(fields.get("Low price"))
    high = _price(fields.get("High price"))
    if low and high:
        return (low + high) / 2
    values = _numbers(str(opportunity.get("entry_zone", "")))
    if len(values) >= 2:
        return (values[0] + values[1]) / 2
    if values:
        return values[0]
    return None


def _stop_from_pct(entry: float | None, text: Any) -> float | None:
    pct = _percent(text)
    if entry is None or pct is None:
        return None
    return entry * (1 - pct / 100)


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
