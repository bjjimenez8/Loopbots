from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from market_data import MarketDataClient, MarketDataConfig
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
        self.market_data = MarketDataClient(
            MarketDataConfig(
                exchange_id=exchange_config["id"],
                enable_rate_limit=exchange_config["enable_rate_limit"],
                sandbox=exchange_config["sandbox"],
                timeframe=exchange_config["timeframe"],
                candle_limit=exchange_config["candle_limit"],
            )
        )
        self.strategy = LoopStrategy(config["strategy"], config["loop_settings"])
        self.trade_manager = TradeManager(
            active_trades_file=str(PROJECT_ROOT / config["storage"]["active_trades_file"]),
            trade_history_file=str(PROJECT_ROOT / config["storage"]["trade_history_file"]),
        )
        self.telegram = TelegramAlertClient(
            bot_token=config["telegram"]["bot_token"],
            chat_id=config["telegram"]["chat_id"],
        )
        self.pairs = config["pairs"]

    async def scan_once(self) -> None:
        logging.info("Starting scan for %d pairs", len(self.pairs))
        for symbol in self.pairs:
            try:
                candles = self.market_data.fetch_ohlcv(symbol)
                active_trade = self.trade_manager.get_active_trade(symbol)

                if active_trade:
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

                    exit_signal = self.strategy.analyze_exit(symbol, candles, active_trade)
                    if exit_signal.signal_type == "EXIT":
                        self.trade_manager.close_trade(exit_signal, exit_signal.reason)
                        await self.telegram.send_exit_alert(exit_signal)
                    continue

                entry_signal = self.strategy.analyze_entry(symbol, candles)
                if entry_signal.signal_type == "ENTER":
                    self.trade_manager.open_trade(entry_signal)
                    await self.telegram.send_enter_alert(entry_signal)
            except Exception:
                logging.exception("Failed to scan %s", symbol)

        logging.info("Scan complete")


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
    scheduler.start()

    await app.scan_once()
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
