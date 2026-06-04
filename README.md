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
  paper_tracker.py
  dashboard.py
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

It also starts a local paper-trading dashboard at `http://127.0.0.1:3000` so you can compare the alerts against what Bitsgap would have done.

## Alert Format

Entry alerts include:

- Coin
- Optimized Bitsgap LOOP preset
- Entry price
- Order distance
- Order count
- Estimated low/high price range
- Safety exit / manual stop-bot price
- Take profit price target
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

The local history is only for paper tracking and tuning. By default the bot prunes paper history older than `30` days, while active alerts stay saved until they close.

## Paper Tracking

Paper tracking uses the same alerts the bot sends:

- `ENTER` opens a paper trade.
- Take profit closes it as a paper win without sending a separate Telegram exit.
- Safety exit closes it as a paper loss and sends the normal `EXIT` alert.

Open `http://127.0.0.1:3000` while the bot is running to see active alerts, closed paper trades, wins, losses, win rate, estimated net return after the fee assumption, average net per trade, average hold time, and best/worst symbols.

## Hetzner Auto Deploy

Loopbots can auto-update on Hetzner whenever `main` is pushed to GitHub.

Run this once on the Hetzner server:

```bash
sudo apt-get update
sudo apt-get install -y git
git clone https://github.com/bjjimenez8/Loopbots.git /opt/loopbots
cd /opt/loopbots
sudo bash deploy/setup_hetzner.sh
```

The setup script installs Loopbots as a `systemd` service and creates a private production config at:

```text
/etc/loopbots/config.yaml
```

Put your real Telegram token/chat ID in that server config. Do not put secrets in GitHub. The service reads that file through `LOOPBOTS_CONFIG`, while runtime data is stored under `/var/lib/loopbots` and logs under `/var/log/loopbots`.

After editing the server config:

```bash
sudo systemctl restart loopbots
sudo systemctl status loopbots
```

To make GitHub deploy automatically, add these repository secrets in GitHub:

```text
HETZNER_HOST      your server IP or hostname
HETZNER_USER      usually root, or a sudo user
HETZNER_SSH_KEY   private SSH key allowed to log in
HETZNER_PORT      optional, defaults to 22
HETZNER_PATH      optional, defaults to /opt/loopbots
```

The workflow `.github/workflows/deploy-to-hetzner.yml` will SSH into the server, pull `origin/main`, reinstall requirements, and restart the `loopbots` service.

For the paper dashboard, the safest setup is to keep it bound to `127.0.0.1` and open it through an SSH tunnel:

```bash
ssh -L 3000:127.0.0.1:3000 root@YOUR_SERVER_IP
```

Then open `http://127.0.0.1:3000` on your own computer.

## Strategy Summary

The default strategy looks for:

- Fast EMA above slow EMA above trend EMA.
- Price reclaiming the fast EMA.
- A recent pullback within configured bounds.
- A small bounce from the recent low.
- RSI in a short-term momentum range.
- Volume near or above its short-term average.

The live scanner can evaluate more than one optimized strategy mode. With the default setup in `config.yaml`, it scans the Kraken-ready coins that held up best in the latest tests:

- `DOGE/USDT`: `10` orders, `1.2%` order distance
- `SOL/USDT`: `10` orders, `1.2%` order distance
- `ETH/USDT`: `10` orders, `2.0%` order distance

The settings stay inside the Bitsgap LOOP manual rules: order count must be even, between `10` and `40`, and order distance must be at least `0.5%`. The strategy also estimates the auto-generated low/high range before alerting and rejects a setup if the take-profit target would sit outside that usable range.

The strategy is intentionally picky about entry location. It avoids alerts when price is too high in the recent range, because the goal is a cleaner push to take profit instead of chasing after the move already happened.

## Backtesting

`backtester.py` is prepared for future backtesting with CSV candle data. CSV files should include:

```text
timestamp,open,high,low,close,volume
```

The backtester currently simulates entries, take-profit exits, and safety exits using the same strategy module as the live scanner.

`run_backtest.py` also estimates LOOP-style grid cycles while an alert is active. It reports the normal entry-to-exit return separately from estimated grid profit, so you can compare a simple trade result against a more Bitsgap-like LOOP result.

For exchange-based history testing, `run_backtest.py` supports splitting the live universe from the history source. That means you can keep `Kraken` as the real trading exchange while using deeper public candles from another exchange such as `OKX`:

```bash
python run_backtest.py --exchange kraken --history-exchange okx --preset dual --days 60 --fee-pct 0.2 --starting-balance 10000 --trade-size 1000
```

For fast local tuning with cached candles:

```bash
python run_backtest.py --exchange kraken --history-exchange okx --preset dual --days 60 --fee-pct 0.2 --starting-balance 10000 --trade-size 1000 --skip-market-validation
```

To test the LOOP money-allocation optimizer across the broader Kraken USDT universe:

```bash
python run_backtest.py --exchange kraken --history-exchange okx --days 60 --fee-pct 0.2 --starting-balance 10000 --trade-size 1000 --all-usdt-pairs --max-pairs 10 --optimize-loop-presets --skip-market-validation
```

The optimizer tests configured LOOP distances, ranks each coin by its best monthly dollar estimate, reports drawdown and monthly returns, and suggests a capped allocation plan. Treat one-trade winners as research only; the allocation report requires a minimum number of trades before suggesting capital.

In that mode:

- `--exchange` controls which symbols must exist on your real trading exchange.
- `--history-exchange` controls where the historical candles come from.
- `--skip-market-validation` uses cached candles without reloading exchange markets.

Latest optimized Kraken/OKX backtest on the configured basket, using a `0.2%` round-trip fee assumption and `$1,000` fixed paper trade size from a `$10,000` example balance:

- `365` days: `111` trades, `59.46%` win rate, `+31.73%` summed net trade return, example account ending equity `$10,317.35`
- `120` days: `28` trades, `67.86%` win rate, `+18.05%` summed net trade return, example account ending equity `$10,180.45`
- `60` days: `7` trades, `71.43%` win rate, `+4.81%` summed net trade return, example account ending equity `$10,048.10`

Per-symbol `365` day results:

- `DOGE/USDT`: `63` trades, `60.32%` win rate, `+15.87%` net return
- `SOL/USDT`: `37` trades, `56.76%` win rate, `+8.59%` net return
- `ETH/USDT`: `11` trades, `63.64%` win rate, `+7.28%` net return

## Important Notes

This project sends alerts only. It does not place exchange orders. Crypto trading is risky; tune and test the strategy before relying on alerts with real capital.
