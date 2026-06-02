from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime
import logging
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from dashboard import DashboardConfig, PaperDashboardServer
from market_regime import mode_allowed
from market_data import MarketDataClient, MarketDataConfig
from news_brief import MorningBriefConfig, MorningBriefService
from paper_tracker import PaperTracker, PaperTrackingConfig
from strategy import LoopStrategy, Signal
from telegram_alerts import TelegramAlertClient
from trade_manager import TradeManager


PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"


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
                    continue

                entry_signal = self._analyze_entry(symbol, candles)
                if entry_signal.signal_type == "ENTER":
                    opened_trade = self.trade_manager.open_trade(entry_signal)
                    if opened_trade:
                        await self.telegram.send_enter_alert(entry_signal)
            except Exception:
                logging.exception("Failed to scan %s", symbol)

        logging.info("Scan complete")
        self._prune_paper_history()

    def refresh_pairs(self) -> None:
        if not self.discovery_config.get("enabled", False):
            self.pairs = list(self.fallback_pairs)
            return

        try:
            discovered_pairs = self.market_data.discover_pairs(self.discovery_config)
            self.pairs = discovered_pairs or list(self.fallback_pairs)
        except Exception:
            logging.exception("Failed to refresh discovered pairs, falling back to configured list")
            self.pairs = list(self.fallback_pairs)

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
        for strategy_mode in self.strategies:
            if not mode_allowed(strategy_mode["mode"], candles, symbol):
                continue
            strategy = strategy_mode["strategy"]
            signal = strategy.analyze_entry(symbol, candles)
            if signal.signal_type == "ENTER":
                return signal
        return Signal("HOLD", symbol=symbol, price=float(candles["close"].iloc[-1]), reason="no entry setup")

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
