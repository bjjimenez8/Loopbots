# Loopbots

Loopbots is a Telegram alert bot for short-term crypto loop strategies. It scans configured USDT pairs every 15 minutes, looks for short-term uptrends, small pullbacks, and bounce opportunities, then sends only actionable alerts:

- `ENTER` alerts when a setup appears.
- `EXIT` alerts when the safety exit is triggered.

It does not send "no trade" messages.

## Monitored Pairs

- BTC/USDT
- ETH/USDT
- SOL/USDT
- DOGE/USDT
- LINK/USDT
- AVAX/USDT
- SUI/USDT

## Project Structure

```text
Loopbots/
  main.py
  strategy.py
  market_data.py
  telegram_alerts.py
  trade_manager.py
  backtester.py
  config.yaml
  requirements.txt
  README.md
  data/
  logs/
```

## Setup

```bash
cd Loopbots
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Edit `config.yaml` and set:

```yaml
telegram:
  bot_token: "YOUR_TELEGRAM_BOT_TOKEN"
  chat_id: "YOUR_TELEGRAM_CHAT_ID"
```

## Run

```bash
python main.py
```

The bot runs one scan immediately, then repeats every 15 minutes.

## Alert Format

`ENTER` alerts include:

- Coin
- Take profit price
- Safety exit price
- Suggested short-term loop settings

`EXIT` alerts include:

- Safety exit triggered
- Stop loop bot

## Storage

Loopbots stores state locally:

- Active trades: `data/active_trades.json`
- Trade history: `data/trade_history.csv`
- Logs: `logs/loopbots.log`

The bot opens one active alert per pair at a time. A pair will not send another `ENTER` alert until its active trade exits.

When take profit is reached, the trade is closed silently in history so the pair can produce future `ENTER` alerts. When the safety exit is reached, the bot sends an `EXIT` alert.

## Strategy Summary

The default strategy looks for:

- Fast EMA above slow EMA above trend EMA.
- Price reclaiming the fast EMA.
- A recent pullback within configured bounds.
- A small bounce from the recent low.
- RSI in a short-term momentum range.
- Volume near or above its short-term average.

These settings are adjustable in `config.yaml`.

## Backtesting

`backtester.py` is prepared for future backtesting with CSV candle data. CSV files should include:

```text
timestamp,open,high,low,close,volume
```

The backtester currently simulates entries, take-profit exits, and safety exits using the same strategy module as the live scanner.

## Important Notes

This project sends alerts only. It does not place exchange orders. Crypto trading is risky; tune and test the strategy before relying on alerts with real capital.
