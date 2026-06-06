# GRID Bots

GRID bots are a live watch feature inside Loopbots.

The bot does not auto-trade. It scans Kraken, waits for historically tested sideways GRID setups, and sends Telegram alerts with the exact Bitsgap fields to enter manually.

## What It Does

- Watches selected Kraken `USDT` spot pairs.
- Uses `1h` candles.
- Looks for sideways/range-bound price action.
- Avoids obvious breakdowns and strong one-way moves.
- Sends a `GRID BOT ENTRY` alert when a setup passes the filter.
- Tracks that GRID alert after it is sent.
- Sends a `GRID BOT EXIT` alert when the watched setup hits take profit or stop loss.
- Records GRID paper entries and exits in `data/grid_trade_history.csv`.
- Sends cooldown-limited no-alert status reports when no setups fire.

## Live Watchlist

These are the current Kraken GRID setups in `config.yaml`:

| Coin | Range | Levels | Historical win rate | Avg 14d return | Worst drawdown | Expected alerts |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `LINK/USDT` | -5% / +35% | 20 | 72% | +2.12% | -8.95% | 1.03/mo |
| `XRP/USDT` | -5% / +35% | 20 | 66.67% | +1.52% | -9.05% | 1.23/mo |
| `LTC/USDT` | -3% / +35% | 20 | 64.1% | +1.49% | -11.52% | 1.6/mo |
| `SOL/USDT` | -8% / +17% | 10 | 64.29% | +1.32% | -8.99% | 1.15/mo |
| `AVAX/USDT` | -25% / +17% | 10 | 66.67% | +0.86% | -7.96% | 0.99/mo |
| `BTC/USDT` | -25% / +17% | 20 | 70% | +0.53% | -6.78% | 2.05/mo |

These are not random coins. They were added because the filtered backtest looked better than the other Kraken pairs tested. Coins that did not hold up were left out.

## Entry Alert

The Telegram entry alert is intentionally short. It only shows the fields needed to create the GRID bot in Bitsgap:

```text
GRID BOT ENTRY
Coin: LINK/USDT
Exchange: Kraken
Low Price: 18.25
High Price: 24.02
Grid Step: 1.5%
Grid Levels: 20
Trailing Up: On
Pump Protection: On
Stop Loss: -5%
Take Profit: +5%
```

When this alert appears, create a manual Bitsgap GRID bot with those settings.

## Exit Alert

The bot tracks GRID entries after alerting. If price reaches the configured take-profit or stop-loss level, it sends:

```text
GRID BOT EXIT
Coin: LINK/USDT
Exchange: Kraken
Action: Stop grid bot
Current Price: 20.42
Reason: Take Profit
```

or:

```text
GRID BOT EXIT
Coin: LINK/USDT
Exchange: Kraken
Action: Stop grid bot
Current Price: 17.10
Reason: Stop Loss
```

When this alert appears, stop the matching manual Bitsgap GRID bot.

## Paper Tracking

GRID paper tracking starts when a `GRID BOT ENTRY` alert is sent.

It records:

- Coin.
- Preset.
- Entry price.
- Low/high range.
- Grid step.
- Grid levels.
- TP/SL.
- Exit price.
- Exit reason.
- Paper return percent.

The history file is:

```text
data/grid_trade_history.csv
```

This is not a full Bitsgap fill-by-fill recreation. It tracks whether the alert hit the watched take-profit or stop-loss first. That is the live proof needed before trusting bigger capital.

## No-Alert Status

If no LOOP or GRID alerts fire, Loopbots can send a cooldown-limited status message:

```text
NO ENTRY
Reason: waiting for cleaner setup.
LOOP closest:
- ETH/USDT 69/80 needs trend, bounce, volume
- DOGE/USDT 49/80 needs trend, volume
GRID closest:
- BTC/USDT trend -2.1%, position 0.45 needs cleaner range
- XRP/USDT trend -3.8%, position 0.35 needs cleaner range
```

The default cooldown is `6` hours.

## Profitability

This feature is built to only alert on setups that were historically profitable in the filtered GRID research.

Honest answer: it is not guaranteed profit.

What is proven so far:

- The live GRID watchlist is based on profitable filtered backtests.
- The filter avoids many bad sideways-looking setups.
- The alerts are selective, so it may send zero alerts in bad conditions.
- The best current historical setup is `LINK/USDT`.

What is not proven yet:

- Real live Bitsgap execution can differ from the local simulator.
- Fees, spread, slippage, and trailing behavior can change results.
- A profitable backtest can stop working in a new market regime.
- This is not enough by itself to reliably make `$3k-$5k/month` without larger capital or a stronger live edge.

Use GRID alerts as a controlled live test first. The goal is to build live proof, not blindly force trades.

## How The Filter Works

The GRID scanner checks the last `14` days and requires:

- Trend return between `-5%` and `+8%`.
- Range size between `5%` and `25%`.
- Low directional efficiency, meaning price is moving around instead of trending hard.
- Current price sitting inside the range, not at the top or bottom edge.
- No active GRID alert already open for that exact setup.
- No recent duplicate alert inside the cooldown window.

## Backtesting

The research tool is:

```powershell
python run_grid_backtest.py
```

Example Kraken optimizer command:

```powershell
python run_grid_backtest.py --exchange kraken --history-exchange okx --days 730 --timeframe 1h --investment 5000 --fee-pct 0.1 --symbols LINK/USDT,XRP/USDT,LTC/USDT,SOL/USDT,AVAX/USDT,BTC/USDT --optimize-grid --hold-days 14 --step-days 5 --launch-filter strict-sideways --take-profit-pct 5 --stop-loss-pct 5 --optimizer-lower-pcts 3,5,8,14,25 --optimizer-upper-pcts 7.5,17,35 --optimizer-levels 10,20,50 --min-rolling-starts 20 --min-win-rate-pct 55 --min-avg-return-pct 0 --min-p10-return-pct -7 --min-avg-monthly-pct 0.1 --top-setups-per-symbol 3
```

The backtester is useful for research and ranking, but Bitsgap's exact internal execution is not perfectly replicated.

## Files

- `grid_watch.py`: live GRID scanner, state tracking, entry/exit detection.
- `telegram_alerts.py`: GRID entry and exit Telegram message format.
- `config.yaml`: live GRID watchlist and setup settings.
- `run_grid_backtest.py`: GRID research and optimizer CLI.
- `data/grid_watch_state.json`: local GRID alert state.
- `data/grid_trade_history.csv`: GRID paper tracking history.
