# Loopbots

Loopbots is a Telegram alert bot for short-term crypto loop strategies. It scans configured USDT pairs every 15 minutes, looks for LOOP-ready uptrends, shallow pullbacks, and bounce confirmations, then sends only actionable alerts.

- `ENTER` alerts when a setup is ready.
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

Entry alerts look like this:

```text
🚨 LOOP BOT ENTRY
Coin: ETH/USDT
Action: Start loop bot / enter trade
Entry: 3842.15
Stop Loss / Safety Exit: 3798.42
Get Out / Take Profit: 3909.76
Reason: Trend up, shallow pullback, bounce confirmed
```

Exit alerts look like this:

```text
⚠️ LOOP BOT EXIT
Coin: ETH/USDT
Action: Stop loop bot / get out
Current Price: 3798.12
Stop Loss Hit: 3798.42
Reason: Safety exit touched
```

A morning brief looks like this:

```text
Good morning.

Morning Crypto Brief
Mood: mostly green and steady.

Market check:
- BTC/USDT: 108245.0000 (+1.24% 24h)
- ETH/USDT: 3892.4400 (+0.85% 24h)
- SOL/USDT: 176.2100 (+2.12% 24h)

Headlines:
1. Example headline
2. Example headline
3. Example headline

Take it easy and wait for clean setups.
```

## Storage

Loopbots stores state locally:

- Active trades: `data/active_trades.json`
- Trade history: `data/trade_history.csv`
- Morning brief state: `data/morning_brief_state.json`
- Logs: `logs/loopbots.log`

The bot opens one active alert per pair at a time. A pair will not send another `ENTER` alert until its active trade exits.

When take profit is reached, the trade is closed silently in history so the pair can produce future `ENTER` alerts. When the safety exit is reached, the bot sends one `EXIT` alert and marks that pair inactive.

## Strategy Summary

The live strategy is a strict short-term LOOP-ready filter:

- Uptrend: fast EMA above slow EMA above trend EMA.
- Pullback: price dips slightly from a recent high, not a breakdown.
- Bounce: price starts reclaiming after the pullback.
- Quality filters: RSI and volume must still support continuation.
- LOOP readiness: the range must support a valid order count, order distance, fee buffer, and reward-to-risk profile.
- Pair-aware tuning: BTC and ETH get a mild relaxation because they move differently from the faster alt pairs.

These settings are adjustable in `config.yaml`, while `run_backtest.py` also supports separate `short` and `mid` research presets without changing the live config.

## Backtesting

`backtester.py` supports CSV-based simulation, and `run_backtest.py` adds reusable public-market backtests with:

- exchange selection
- short vs mid preset comparison
- cached candle downloads in `data/backtests/`
- fixed-size portfolio balance simulation

Example:

```bash
python run_backtest.py --exchange okx --days 60 --fee-pct 0.2 --preset short --starting-balance 10000 --trade-size 1000
```

CSV candle files should use:

```text
timestamp,open,high,low,close,volume
```

## Important Notes

This project sends alerts only. It does not place exchange orders. Crypto trading is risky; tune and test the strategy before relying on alerts with real capital.
