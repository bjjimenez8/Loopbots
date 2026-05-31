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

        settings = signal.loop_settings or {}
        message = (
            "ENTER ALERT\n"
            f"Coin: {signal.symbol}\n"
            f"Take profit price: {signal.take_profit_price}\n"
            f"Safety exit price: {signal.safety_exit_price}\n"
            "Suggested short-term loop settings:\n"
            f"- Profit target: {settings.get('suggested_profit_pct', 0) * 100:.2f}%\n"
            f"- Safety exit: {settings.get('suggested_safety_exit_pct', 0) * 100:.2f}%\n"
            f"- Max loops: {settings.get('max_loop_count')}\n"
            f"- Quote amount: {settings.get('quote_amount_usdt')} USDT"
        )
        await self._send(message)

    async def send_exit_alert(self, signal: Signal) -> None:
        if signal.signal_type != "EXIT":
            return

        message = (
            "EXIT ALERT\n"
            f"Coin: {signal.symbol}\n"
            "Safety exit triggered\n"
            "Stop loop bot"
        )
        await self._send(message)

    async def _send(self, message: str) -> None:
        if not self.enabled or self.bot is None:
            LOGGER.warning("Telegram disabled. Alert not sent:\n%s", message)
            return

        await self.bot.send_message(chat_id=self.chat_id, text=message)
