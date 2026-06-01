# Loopbots

Loopbots is a Telegram alert bot for short-term crypto loop strategies. It scans configured USDT pairs every 15 minutes, looks for short-term uptrends, small pullbacks, and bounce opportunities, then sends only actionable alerts:

- `ENTER` alerts when a setup appears.
- `EXIT` alerts when the safety exit is triggered.

It does not send "no trade" messages.

## Monitored Pairs

Loopbots can scan a configured fallback list or automatically discover liquid, volatile `USDT` spot pairs from the live exchange.

## Project Structure

```text
Loopbots/
  main.py
  strategy.py
  market_data.py
  market_regime.py
  telegram_alerts.py
  trade_manager.py
  backtester.py
  run_backtest.py
  news_brief.py
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

It also sends a small morning crypto brief every day at `8:00 AM` Pacific with a few market lines and crypto headlines.

## Alert Format

Entry alerts include:

- Coin
- Preset (`Short-term` or `Mid-term`)
- Entry price
- Safety exit price
- Take profit price
- Short reason

Exit alerts include:

- Coin
- Current price
- Safety exit level
- Reason

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

The live scanner can evaluate more than one strategy mode. With the default setup in `config.yaml`, it tries:

- `Short-term` only for tighter, sideways/range-friendly LOOP opportunities on supported alt pairs
- `Mid-term` for steadier continuation setups and as the default fallback

Whichever preset actually passes is the one shown in the Telegram alert.

## Backtesting

`backtester.py` is prepared for future backtesting with CSV candle data. CSV files should include:

```text
timestamp,open,high,low,close,volume
```

The backtester currently simulates entries, take-profit exits, and safety exits using the same strategy module as the live scanner.

For exchange-based history testing, `run_backtest.py` supports splitting the live universe from the history source. That means you can keep `Kraken` as the real trading exchange while using deeper public candles from another exchange such as `OKX`:

```bash
python run_backtest.py --exchange kraken --history-exchange okx --preset dual --days 60 --fee-pct 0.2 --starting-balance 10000 --trade-size 1000
```

In that mode:

- `--exchange` controls which symbols must exist on your real trading exchange.
- `--history-exchange` controls where the historical candles come from.

## Important Notes

This project sends alerts only. It does not place exchange orders. Crypto trading is risky; tune and test the strategy before relying on alerts with real capital.
