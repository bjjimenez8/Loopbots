# GRID Bots

GRID bots are a live watch feature inside Loopbots.

The bot does not auto-trade. It scans Kraken, auto-discovers researched hot `USD` and `USDC` GRID setups, waits for live sideways/range quality, and sends Telegram alerts with the exact Bitsgap fields to enter manually.

## What It Does

- Watches researched Kraken `USD` and `USDC` spot profiles.
- Auto-discovers the matching live Kraken pairs from the exchange markets.
- Uses `1h` candles.
- Looks for sideways/range-bound price action.
- Avoids obvious breakdowns and strong one-way moves.
- Sends a `GRID BOT ENTRY` alert when a setup passes the filter.
- Includes Bitsgap stop loss and take profit settings in the entry alert.
- Opens an internal GRID paper trade after each entry alert.
- Silently records paper GRID take-profit or stop-loss closes for stats.
- Uses cooldown tracking after an alert so it does not spam the same setup.
- Sends cooldown-limited `BOT STATUS` reports when no setups fire.

## Research Watchlist

These are research candidates in `config.yaml`. None currently meets the strengthened proof standard.

| Coin | Quote | Range | Levels | Historical win rate | Est. monthly | Worst drawdown | Filter |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| `PEPE` | `USD` | -8% / +50% | 10 | 100% | +27.85% | -6.64% | strict sideways |
| `JTO` | `USD` | -5% / +50% | 10 | 90.91% | +22.42% | -7.07% | sideways |
| `INJ` | `USD` | -8% / +50% | 10 | 63.16% | +9.79% | -9.06% | sideways |
| `XCN` | `USD` | -8% / +35% | 10 | 72.22% | +14.99% | -10.22% | sideways |
| `ETH` | `USDC` | -5% / +50% | 10 | 63.33% | +9.57% | -9.62% | strict sideways |
| `IDEX` | `USD` | -10% / +50% | 10 | 100% | +29.66% | -5.58% | experimental sideways |

The July 2026 audit supersedes the older headline statistics above. It tested 13 liquid coins over 730 days with non-overlapping runs, a year-one/year-two split, and a 0.25% per-order fee assumption. No setup delivered both serious returns and robust out-of-sample proof. BTC remained slightly positive in both years but with too little return; BNB and LTC failed the untouched year.

`IDEX/USD` is marked experimental because the backtest result was strong but only had `3` valid historical starts. It is watched and paper-tracked automatically, but should not be sized like a proven setup until live paper and real Bitsgap results confirm it.

The scanner also has an `Auto Hot GRID` lane for new Kraken `USD`/`USDC` coins. Those candidates must pass stricter volume, volatility, plain-symbol, and strict-sideways filters before alerting. They use `-8% / +35%`, `10` levels, `+8%` take profit, and `-5%` stop loss. They are not treated as per-coin proven profiles until research confirms them.

## Entry Alert

The Telegram entry alert is intentionally short. It only shows the fields needed to create the GRID bot in Bitsgap:

```text
GRID BOT ENTRY
Coin: JTO/USD
Exchange: Kraken
Low Price: 0.519158
High Price: 0.82077
Grid Levels: 10
Grid Step: Roughly 4.7%
Order Size Currency: USD
Trailing Up: On
Pump Protection: On
Trailing Down: Off
Stop Loss: On (-5%)
Take Profit: On (+8%)
```

When this alert appears, create a manual Bitsgap GRID bot with those settings.

GRID bots do not send separate exit alerts. Bitsgap handles the stop loss and take profit from the setup alert. Loopbots still paper-tracks those GRID TP/SL outcomes internally. LOOP bots are the ones that still use separate Telegram exit alerts.

## Bot Status

If no LOOP or GRID alerts fire, Loopbots can send a cooldown-limited status message:

```text
BOT STATUS
No entries this scan.
Why: waiting for cleaner setup.
Closest LOOP: LTC/USDT 78/100, SOL/USDT 70/100
Closest GRID: JTO/USD 67/100, PEPE/USD 55/100
```

The status is sent at most once per day after `8:00 PM` Pacific, and only when no real LOOP or GRID alert fired on that scan.

## Profitability

This feature is built to only alert on setups that were historically profitable in the filtered GRID research.

Honest answer: it is not guaranteed profit.

What is supported so far:

- The scanner and backtester correctly reject many unsuitable setups.
- The filter avoids many bad sideways-looking setups.
- The alerts are selective, so it may send zero alerts in bad conditions.
- No coin currently satisfies the full Ready Now proof gate.

What is not proven yet:

- Real live Bitsgap execution can differ from the local simulator.
- Fees, spread, slippage, and trailing behavior can change results.
- A profitable backtest can stop working in a new market regime.
- This is not enough by itself to reliably make `$3k-$5k/month` without larger capital or a stronger live edge.

Use GRID alerts as a controlled live test first. The goal is to build live proof, not blindly force trades.

## How The Filter Works

The GRID scanner checks the last `14` days. Strict-sideways profiles require:

- Trend return between `-5%` and `+8%`.
- Range size between `5%` and `25%`.
- Low directional efficiency, meaning price is moving around instead of trending hard.
- Current price sitting inside the range, not at the top or bottom edge.
- No recent duplicate alert inside the cooldown window.

Regular sideways hot profiles allow a wider range and slightly more trend, but still reject ugly breakdowns, one-way pumps, and bad range position.

## Backtesting

The research tool is:

```powershell
python run_grid_backtest.py
```

Example Kraken optimizer command:

```powershell
python run_grid_backtest.py --exchange kraken --history-exchange okx --days 730 --timeframe 1h --investment 100 --fee-pct 0.25 --symbols BTC/USDT,ETH/USDT,SOL/USDT,DOGE/USDT,LINK/USDT,LTC/USDT,ADA/USDT,ALGO/USDT,AVAX/USDT,BNB/USDT,XRP/USDT,TON/USDT,XAUT/USDT --optimize-grid --hold-days 10 --step-days 10 --launch-filter strict-sideways --take-profit-pct 8 --stop-loss-pct 5 --min-rolling-starts 15 --min-win-rate-pct 60 --min-avg-return-pct 3 --min-p10-return-pct -7 --min-avg-monthly-pct 6
```

The backtester is useful for research and ranking, but Bitsgap's exact internal execution is not perfectly replicated.

## Files

- `grid_watch.py`: live Hot GRID scanner, auto-discovery, cooldown tracking, and entry alert building.
- `telegram_alerts.py`: GRID entry Telegram message format.
- `config.yaml`: Hot GRID profiles and scanner settings.
- `run_grid_backtest.py`: GRID research and optimizer CLI.
- `data/grid_watch_state.json`: local GRID alert state.
- `data/grid_trade_history.csv`: GRID entry alert history.
