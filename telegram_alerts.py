from __future__ import annotations

import logging

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
            "🚨 LOOP BOT ENTRY\n"
            f"Coin: {signal.symbol}\n"
            f"Preset: {loop_plan.get('preset_name', 'Mid-term')}\n"
            f"Bitsgap Default: {loop_plan.get('order_count', 'n/a')} orders / {loop_plan.get('order_distance_pct', 'n/a')}% spacing\n"
            "Action: Start loop bot / enter trade\n"
            f"Entry: {signal.price}\n"
            f"Stop Loss / Safety Exit: {signal.safety_exit_price}\n"
            f"Get Out / Take Profit: {signal.take_profit_price}\n"
            f"Reason: {signal.reason}"
        )
        await self._send(message)

    async def send_exit_alert(self, signal: Signal) -> None:
        if signal.signal_type != "EXIT":
            return

        message = (
            "⚠️ LOOP BOT EXIT\n"
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
