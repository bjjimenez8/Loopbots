from __future__ import annotations

import logging
from typing import Any

from telegram import Bot

from strategy import Signal


LOGGER = logging.getLogger(__name__)


class TelegramAlertClient:
    def __init__(self, bot_token: str, chat_id: str) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = bool(bot_token and chat_id and "PUT_" not in bot_token and "PUT_" not in chat_id)
        self.bot = Bot(token=bot_token) if self.enabled else None

    async def send_enter_alert(self, signal: Signal) -> None:
        if signal.signal_type != "ENTER":
            return

        loop_plan = (signal.loop_settings or {}).get("loop_plan", {})
        message = (
            "LOOP BOT ENTRY\n"
            f"Coin: {signal.symbol}\n"
            f"Method: {(signal.loop_settings or {}).get('method_name', 'Trend pullback')}\n"
            f"Preset: {loop_plan.get('preset_name', 'Optimized')}\n"
            "Action: Start loop bot / enter trade\n"
            f"Entry: {signal.price}\n"
            "Bitsgap Setup: Manual LOOP\n"
            f"Order Distance: {loop_plan.get('order_distance_pct', 'n/a')}%\n"
            f"Order Count: {loop_plan.get('order_count', 'n/a')}\n"
            f"Auto Range Reference: {loop_plan.get('estimated_low_price', 'auto')} - {loop_plan.get('estimated_high_price', 'auto')}\n"
            "Range Note: Bitsgap sets low/high automatically\n"
            f"Score: {loop_plan.get('setup_score', 'n/a')}/100\n"
            f"Safety Exit / Stop Bot: {signal.safety_exit_price}\n"
            f"Take Profit Price Target: {signal.take_profit_price}\n"
            f"Reason: {signal.reason}"
        )
        await self._send(message)

    async def send_grid_alert(self, grid_plan: dict[str, Any]) -> None:
        message = (
            "GRID BOT ENTRY\n"
            f"Coin: {grid_plan.get('symbol', 'n/a')}\n"
            f"Method: {grid_plan.get('method_name', 'Hot GRID')}\n"
            f"Preset: {grid_plan.get('preset_name', 'Optimized GRID')}\n"
            "Action: Create grid bot\n"
            f"Exchange: {grid_plan.get('exchange', 'Kraken')}\n"
            f"Investment: {grid_plan.get('investment_usdt', 'n/a')} USDT\n"
            "Bitsgap Setup: Manual GRID\n"
            f"Low Price: {grid_plan.get('low_price', 'n/a')}\n"
            f"High Price: {grid_plan.get('high_price', 'n/a')}\n"
            f"Grid Step: {grid_plan.get('grid_step_pct', 'n/a')}%\n"
            f"Grid Levels: {grid_plan.get('levels', 'n/a')}\n"
            f"Order Size Currency: {grid_plan.get('order_size_currency', 'USDT')}\n"
            f"Trailing Up: {grid_plan.get('trailing_up', 'On')}\n"
            f"Pump Protection: {grid_plan.get('pump_protection', 'On')}\n"
            f"Stop Loss: {grid_plan.get('stop_loss_pct', 'n/a')}%\n"
            f"Take Profit: {grid_plan.get('take_profit_pct', 'n/a')}%\n"
            f"Backtest: {grid_plan.get('backtest_return_pct', 'n/a')}% / {grid_plan.get('backtest_days', 'n/a')}d\n"
            f"Win Rate: {grid_plan.get('historical_win_rate_pct', 'n/a')}%\n"
            f"Expected Alerts: {grid_plan.get('historical_alerts_per_month', 'n/a')}/mo\n"
            f"Est. Profit: {grid_plan.get('estimated_profit_usdt', 'n/a')} USDT\n"
            f"Max Drawdown: {grid_plan.get('max_drawdown_pct', 'n/a')}%\n"
            f"Score: {grid_plan.get('score', 'n/a')}/100"
        )
        await self._send(message)

    async def send_exit_alert(self, signal: Signal) -> None:
        if signal.signal_type != "EXIT":
            return

        message = (
            "LOOP BOT EXIT\n"
            f"Coin: {signal.symbol}\n"
            "Action: Stop loop bot / get out\n"
            f"Current Price: {signal.price}\n"
            f"Stop Loss Hit: {signal.safety_exit_price}\n"
            "Reason: Safety exit touched"
        )
        await self._send(message)

    async def send_morning_brief(self, message: str) -> None:
        await self._send(message)

    async def _send(self, message: str) -> None:
        if not self.enabled or self.bot is None:
            LOGGER.warning("Telegram disabled. Alert not sent:\n%s", message)
            return

        await self.bot.send_message(chat_id=self.chat_id, text=message)
