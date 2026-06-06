from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
import logging
import os
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from dashboard import DashboardConfig, PaperDashboardServer
from grid_watch import GridWatchService
from market_regime import mode_allowed
from market_data import MarketDataClient, MarketDataConfig
from news_brief import MorningBriefConfig, MorningBriefService
from paper_tracker import PaperTracker, PaperTrackingConfig
from strategy import LoopStrategy, Signal
from telegram_alerts import TelegramAlertClient
from trade_manager import TradeManager


PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("LOOPBOTS_CONFIG", PROJECT_ROOT / "config.yaml")).expanduser()


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        return yaml.safe_load(file)


def setup_logging(log_file: str) -> None:
    log_path = PROJECT_ROOT / log_file
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


class LoopbotsApp:
    def __init__(self, config: dict[str, Any]) -> None:
        exchange_config = config["exchange"]
        self.discovery_config = config.get("pair_discovery", {})
        self.market_data = MarketDataClient(
            MarketDataConfig(
                exchange_id=exchange_config["id"],
                enable_rate_limit=exchange_config["enable_rate_limit"],
                sandbox=exchange_config["sandbox"],
                timeframe=exchange_config["timeframe"],
                candle_limit=exchange_config["candle_limit"],
                discovery_refresh_minutes=self.discovery_config.get("refresh_minutes", 60),
            )
        )
        self.strategies = self._build_strategies(config)
        self.trade_manager = TradeManager(
            active_trades_file=str(PROJECT_ROOT / config["storage"]["active_trades_file"]),
            trade_history_file=str(PROJECT_ROOT / config["storage"]["trade_history_file"]),
            fee_pct=float(config.get("loop_settings", {}).get("assumed_round_trip_fee_pct", 0.2)),
        )
        self.telegram = TelegramAlertClient(
            bot_token=config["telegram"]["bot_token"],
            chat_id=config["telegram"]["chat_id"],
        )
        self.grid_watch = GridWatchService.from_config(config.get("grid_watch", {}), PROJECT_ROOT)
        self.status_config = config.get("status_report", {})
        self.status_state_path = PROJECT_ROOT / self.status_config.get("state_file", "data/status_report_state.json")
        self.status_state_path.parent.mkdir(parents=True, exist_ok=True)
        self.fallback_pairs = list(config["pairs"])
        self.pairs = list(self.fallback_pairs)
        morning_config = config.get("morning_brief", {})
        self.morning_brief = MorningBriefService(
            exchange=self.market_data.exchange,
            pairs=self.pairs,
            config=MorningBriefConfig(
                enabled=morning_config.get("enabled", True),
                hour=morning_config.get("hour", 8),
                minute=morning_config.get("minute", 0),
                timezone=morning_config.get("timezone", config["scheduler"]["timezone"]),
                headline_count=morning_config.get("headline_count", 3),
                state_file=str(PROJECT_ROOT / morning_config.get("state_file", "data/morning_brief_state.json")),
                headline_feed_url=morning_config.get(
                    "headline_feed_url",
                    "https://www.coindesk.com/arc/outboundfeeds/rss/",
                ),
            ),
        )
        paper_config = config.get("paper_tracking", {})
        self.paper_tracker = PaperTracker(
            active_trades_file=str(PROJECT_ROOT / config["storage"]["active_trades_file"]),
            trade_history_file=str(PROJECT_ROOT / config["storage"]["trade_history_file"]),
            config=PaperTrackingConfig(
                enabled=paper_config.get("enabled", True),
                lookback_days=paper_config.get("lookback_days", 7),
                retention_days=paper_config.get("retention_days", 30),
                fee_pct=float(config.get("loop_settings", {}).get("assumed_round_trip_fee_pct", 0.2)),
            ),
        )
        dashboard_config = config.get("dashboard", {})
        self.dashboard = PaperDashboardServer(
            tracker=self.paper_tracker,
            config=DashboardConfig(
                enabled=dashboard_config.get("enabled", True),
                host=dashboard_config.get("host", "127.0.0.1"),
                port=int(dashboard_config.get("port", 3000)),
                refresh_seconds=int(dashboard_config.get("refresh_seconds", 30)),
            ),
        )

    def start_dashboard(self) -> None:
        self.dashboard.start()

    async def scan_once(self) -> None:
        self.refresh_pairs()
        logging.info("Starting scan for %d pairs", len(self.pairs))
        loop_entry_count = 0
        loop_exit_count = 0
        loop_diagnostics: list[dict[str, Any]] = []
        for symbol in self.pairs:
            try:
                candles = self.market_data.fetch_ohlcv(symbol)
                active_trade = self.trade_manager.get_active_trade(symbol)

                if active_trade:
                    active_trade = self.trade_manager.update_paper_grid(symbol, candles.iloc[-1]) or active_trade
                    current_price = float(candles["close"].iloc[-1])
                    take_profit_price = float(active_trade["take_profit_price"])
                    if current_price >= take_profit_price:
                        take_profit_signal = Signal(
                            "HOLD",
                            symbol=symbol,
                            price=current_price,
                            take_profit_price=take_profit_price,
                            safety_exit_price=float(active_trade["safety_exit_price"]),
                            reason="take profit reached",
                        )
                        self.trade_manager.close_trade(
                            take_profit_signal,
                            "take profit reached",
                            event="TAKE_PROFIT",
                        )
                        continue

                    exit_signal = self.strategies[0]["strategy"].analyze_exit(symbol, candles, active_trade)
                    if exit_signal.signal_type == "EXIT":
                        self.trade_manager.close_trade(exit_signal, exit_signal.reason)
                        await self.telegram.send_exit_alert(exit_signal)
                        loop_exit_count += 1
                    continue

                loop_diagnostics.extend(self._loop_diagnostics(symbol, candles))
                entry_signal = self._analyze_entry(symbol, candles)
                if entry_signal.signal_type == "ENTER":
                    opened_trade = self.trade_manager.open_trade(entry_signal)
                    if opened_trade:
                        await self.telegram.send_enter_alert(entry_signal)
                        loop_entry_count += 1
            except Exception:
                logging.exception("Failed to scan %s", symbol)

        logging.info("Scan complete")
        grid_counts = await self.scan_grid_watch()
        total_alerts = loop_entry_count + loop_exit_count + grid_counts["entries"] + grid_counts["exits"]
        if total_alerts == 0:
            await self.maybe_send_no_alert_status(loop_diagnostics)
        self._prune_paper_history()

    async def scan_grid_watch(self) -> dict[str, int]:
        counts = {"entries": 0, "exits": 0}
        if not self.grid_watch.config.enabled:
            return counts

        try:
            exit_alerts = self.grid_watch.find_exit_alerts()
            alerts = self.grid_watch.find_alerts()
        except Exception:
            logging.exception("Failed to scan GRID watch")
            return counts

        for alert in exit_alerts:
            await self.telegram.send_grid_exit_alert(alert)
        for alert in alerts:
            await self.telegram.send_grid_alert(alert)
        counts["entries"] = len(alerts)
        counts["exits"] = len(exit_alerts)
        if exit_alerts:
            logging.info("Sent %d GRID exit alerts", len(exit_alerts))
        if alerts:
            logging.info("Sent %d GRID watch alerts", len(alerts))
        return counts

    async def maybe_send_no_alert_status(self, loop_diagnostics: list[dict[str, Any]]) -> None:
        if not self.status_config.get("enabled", True):
            return
        interval_hours = float(self.status_config.get("interval_hours", 6))
        if not self._status_report_due(interval_hours):
            return

        message = self._build_no_alert_status(loop_diagnostics)
        await self.telegram.send_status_report(message)
        self._mark_status_report_sent()

    def _status_report_due(self, interval_hours: float) -> bool:
        if interval_hours <= 0:
            return True
        if not self.status_state_path.exists():
            return True
        try:
            state = json.loads(self.status_state_path.read_text(encoding="utf-8"))
            last_sent_at = datetime.fromisoformat(str(state.get("last_sent_at", "")))
        except (ValueError, json.JSONDecodeError, OSError):
            return True
        if last_sent_at.tzinfo is None:
            last_sent_at = last_sent_at.replace(tzinfo=UTC)
        return datetime.now(UTC) - last_sent_at >= timedelta(hours=interval_hours)

    def _mark_status_report_sent(self) -> None:
        self.status_state_path.write_text(
            json.dumps({"last_sent_at": datetime.now(UTC).isoformat()}, indent=2),
            encoding="utf-8",
        )

    def _build_no_alert_status(self, loop_diagnostics: list[dict[str, Any]]) -> str:
        best_loop = max(loop_diagnostics, key=lambda row: row.get("score", 0), default={})
        grid_diagnostics = self.grid_watch.diagnostics() if self.grid_watch.config.enabled else []
        grid_ready = [row for row in grid_diagnostics if row.get("ready")]
        grid_closest = min(
            grid_diagnostics,
            key=lambda row: (
                abs(float(row.get("trend_return_pct", 999))),
                abs(float(row.get("directional_efficiency", 999))),
            ),
            default={},
        )
        grid_paper = self.grid_watch.paper_snapshot() if self.grid_watch.config.enabled else {}

        lines = [
            "BOT STATUS",
            "No entries right now.",
            (
                f"LOOP: best {best_loop.get('symbol', 'n/a')} "
                f"{best_loop.get('score', 0)}/80"
            ),
            f"GRID: {len(grid_ready)}/{len(grid_diagnostics)} ready",
        ]
        if grid_closest:
            lines.append(
                "GRID closest: "
                f"{grid_closest.get('symbol')} "
                f"trend {grid_closest.get('trend_return_pct')}%, "
                f"position {grid_closest.get('range_position')}"
            )
        lines.append(
            "GRID paper: "
            f"{grid_paper.get('closed', 0)} closed, "
            f"WR {float(grid_paper.get('win_rate_pct', 0.0)):.2f}%"
        )
        lines.append("Reason: waiting for cleaner setup.")
        return "\n".join(lines)

    def refresh_pairs(self) -> None:
        if not self.discovery_config.get("enabled", False):
            self.pairs = list(self.fallback_pairs)
            return

        try:
            discovered_pairs = self.market_data.discover_pairs(self.discovery_config)
            self.pairs = self._merge_pairs(discovered_pairs, self.fallback_pairs)
        except Exception:
            logging.exception("Failed to refresh discovered pairs, falling back to configured list")
            self.pairs = list(self.fallback_pairs)

    @staticmethod
    def _merge_pairs(primary_pairs: list[str], fallback_pairs: list[str]) -> list[str]:
        merged_pairs = []
        for symbol in [*primary_pairs, *fallback_pairs]:
            if symbol not in merged_pairs:
                merged_pairs.append(symbol)
        return merged_pairs

    async def send_morning_brief(self) -> None:
        if not self.morning_brief.config.enabled:
            return

        local_now = datetime.now(ZoneInfo(self.morning_brief.config.timezone))
        local_date = local_now.date().isoformat()
        if not self.morning_brief.should_send_today(local_date):
            return

        try:
            message = self.morning_brief.build_brief()
            await self.telegram.send_morning_brief(message)
            self.morning_brief.mark_sent(local_date)
            logging.info("Morning brief sent for %s", local_date)
        except Exception:
            logging.exception("Failed to send morning brief")

    def _prune_paper_history(self) -> None:
        if not self.paper_tracker.config.enabled:
            return
        removed_count = self.trade_manager.prune_history(self.paper_tracker.config.retention_days)
        if removed_count:
            logging.info("Pruned %d old paper history rows", removed_count)

    def _analyze_entry(self, symbol: str, candles: Any) -> Signal:
        entry_candidates: list[Signal] = []
        for strategy_mode in self.strategies:
            if not mode_allowed(strategy_mode["mode"], candles, symbol):
                continue
            strategy = strategy_mode["strategy"]
            signal = strategy.analyze_entry(symbol, candles)
            if signal.signal_type == "ENTER":
                entry_candidates.append(signal)

        if entry_candidates:
            return max(entry_candidates, key=self._entry_score)
        return Signal("HOLD", symbol=symbol, price=float(candles["close"].iloc[-1]), reason="no entry setup")

    def _loop_diagnostics(self, symbol: str, candles: Any) -> list[dict[str, Any]]:
        results = []
        for strategy_mode in self.strategies:
            if not mode_allowed(strategy_mode["mode"], candles, symbol):
                continue
            strategy = strategy_mode["strategy"]
            try:
                results.append(self._loop_strategy_diagnostic(symbol, candles, strategy_mode, strategy))
            except Exception:
                logging.exception("Failed to build LOOP diagnostics for %s", symbol)
        return results

    @staticmethod
    def _loop_strategy_diagnostic(symbol: str, candles: Any, strategy_mode: dict[str, Any], strategy: LoopStrategy) -> dict[str, Any]:
        df = strategy._with_indicators(candles)
        if len(df) < strategy._minimum_candles:
            return {"symbol": symbol, "mode": strategy_mode["mode"].get("name", ""), "score": 0, "reason": "not enough data"}

        latest = df.iloc[-1]
        previous = df.iloc[-2]
        recent = df.iloc[-strategy.config["pullback_lookback"] :]
        range_window = df.iloc[-strategy._range_lookback :]
        price = float(latest["close"])
        atr = float(latest["atr"])
        range_low = float(range_window["low"].min())
        range_high = float(range_window["high"].max())
        range_span = max(range_high - range_low, 0.0)
        range_position = ((price - range_low) / range_span) if range_span else 1.0
        profile = strategy._symbol_profile(symbol)

        trend_ok = latest["ema_fast"] > latest["ema_slow"] > latest["ema_trend"] and latest["ema_trend"] > previous["ema_trend"]
        price_reclaimed_fast_ema = latest["close"] > latest["ema_fast"] * profile["ema_reclaim_buffer"]
        recent_high = float(recent["high"].max())
        pullback_pct = (recent_high - price) / recent_high if recent_high else 0.0
        pullback_ok = strategy.config["pullback_min_pct"] <= pullback_pct <= strategy.config["pullback_max_pct"]
        bounce_ok = (
            latest["close"] >= latest["low"] * (1 + (strategy.config["bounce_confirmation_pct"] * profile["bounce_multiplier"]))
            and latest["close"] >= previous["close"] * profile["previous_close_buffer"]
            and latest["close"] >= latest["open"] * profile["open_buffer"]
        )
        rsi_ok = (
            (strategy.config["min_rsi"] - profile["rsi_low_buffer"])
            <= latest["rsi"]
            <= (strategy.config["max_rsi"] + profile["rsi_high_buffer"])
        )
        volume_ok = latest["volume_ratio"] >= max(strategy.config["min_volume_ratio"] - profile["volume_buffer"], 0.6)
        breakdown_ok = strategy._breakdown_ok(df)
        loop_plan = strategy._build_loop_plan(range_window, price, atr)
        loop_ready = bool(loop_plan) and strategy._loop_ready(loop_plan, price, range_position, profile)
        score = strategy._setup_score(
            latest=latest,
            trend_ok=trend_ok,
            price_reclaimed_fast_ema=price_reclaimed_fast_ema,
            pullback_ok=pullback_ok,
            bounce_ok=bounce_ok,
            rsi_ok=rsi_ok,
            volume_ok=volume_ok,
            loop_plan=loop_plan,
            range_position=range_position,
        )
        failures = [
            name
            for name, passed in {
                "trend": trend_ok,
                "reclaim": price_reclaimed_fast_ema,
                "pullback": pullback_ok,
                "bounce": bounce_ok,
                "rsi": rsi_ok,
                "volume": volume_ok,
                "breakdown": breakdown_ok,
                "loop_ready": loop_ready,
                "score": score >= float(strategy.config.get("min_signal_score", 0.0)),
            }.items()
            if not passed
        ]
        return {
            "symbol": symbol,
            "mode": strategy_mode["mode"].get("name", ""),
            "score": score,
            "price": price,
            "reason": ", ".join(failures) if failures else "READY",
        }

    @staticmethod
    def _entry_score(signal: Signal) -> tuple[float, float, float]:
        loop_plan = (signal.loop_settings or {}).get("loop_plan", {})
        setup_score = float(loop_plan.get("setup_score") or 0.0)
        reward_to_risk = float(loop_plan.get("reward_to_risk") or 0.0)
        order_distance_pct = float(loop_plan.get("order_distance_pct") or 0.0)
        return setup_score, reward_to_risk, order_distance_pct

    def _build_strategies(self, config: dict[str, Any]) -> list[dict[str, Any]]:
        strategy_modes = config.get("strategy_modes") or self._default_strategy_modes()
        strategies: list[dict[str, Any]] = []
        for mode in strategy_modes:
            strategy_config = deepcopy(config["strategy"])
            strategy_config.update(mode.get("strategy_overrides", {}))
            loop_settings = deepcopy(config["loop_settings"])
            loop_settings.update(mode.get("loop_settings", {}))
            strategies.append({"mode": mode, "strategy": LoopStrategy(strategy_config, loop_settings)})
        return strategies

    @staticmethod
    def _default_strategy_modes() -> list[dict[str, Any]]:
        return [
            {
                "name": "short",
                "market_type": "sideways",
                "allowed_base_assets": ["DOGE", "LINK", "SOL"],
                "market_type_rules": {
                    "lookback": 96,
                    "min_range_width_pct": 3.0,
                    "max_range_width_pct": 9.0,
                    "max_ema_slope_pct": 0.7,
                    "min_support_touches": 4,
                    "min_resistance_touches": 4,
                    "min_range_position": 0.2,
                    "max_range_position": 0.6,
                },
                "strategy_overrides": {
                    "pullback_lookback": 5,
                    "pullback_max_pct": 0.028,
                    "bounce_confirmation_pct": 0.0012,
                    "min_volume_ratio": 0.8,
                    "max_active_minutes": 240,
                },
                "loop_settings": {
                    "preset_name": "Short-term",
                    "order_distance_pct": 1.0,
                    "order_count": 10,
                },
            },
            {
                "name": "mid",
                "market_type": "any",
                "strategy_overrides": {
                    "pullback_lookback": 6,
                    "pullback_max_pct": 0.035,
                    "bounce_confirmation_pct": 0.0015,
                    "min_volume_ratio": 0.85,
                    "max_active_minutes": 180,
                },
                "loop_settings": {
                    "preset_name": "Mid-term",
                    "order_distance_pct": 1.5,
                    "order_count": 10,
                },
            },
        ]


async def main() -> None:
    config = load_config()
    setup_logging(config["storage"]["log_file"])
    app = LoopbotsApp(config)

    scheduler = AsyncIOScheduler(timezone=config["scheduler"]["timezone"])
    scheduler.add_job(
        app.scan_once,
        trigger=IntervalTrigger(minutes=config["scheduler"]["interval_minutes"]),
        id="loopbots_scan",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.add_job(
        app.send_morning_brief,
        trigger=CronTrigger(
            hour=app.morning_brief.config.hour,
            minute=app.morning_brief.config.minute,
            timezone=app.morning_brief.config.timezone,
        ),
        id="loopbots_morning_brief",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.start()

    app.start_dashboard()
    await app.scan_once()
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
