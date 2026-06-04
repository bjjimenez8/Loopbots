# LOOP Optimizer Summary

Run date: 2026-06-03

Command:

```powershell
python run_backtest.py --exchange kraken --history-exchange okx --days 120 --fee-pct 0.2 --starting-balance 10000 --trade-size 1000 --all-usdt-pairs --max-pairs 15 --optimize-loop-presets --skip-market-validation
```

## Best Qualified Allocation

These passed the minimum allocation threshold of 3 historical trades:

| Symbol | Best LOOP Distance | Trades | Wins | Losses | Win Rate | Net Return | Est. Monthly Profit per $1k |
|---|---:|---:|---:|---:|---:|---:|---:|
| DOGE/USDT | 1.2% | 12 | 9 | 3 | 75.00% | 10.46% | $26.14 |
| SOL/USDT | 2.0% | 4 | 4 | 0 | 100.00% | 9.89% | $24.73 |
| LTC/USDT | 1.0% | 9 | 7 | 2 | 77.78% | 4.83% | $12.07 |
| ALGO/USDT | 2.0% | 9 | 5 | 4 | 55.56% | 2.77% | $6.93 |

## Final Tuned Live Config Backtest

After wiring only the qualified symbol-specific modes and removing the generic catch-all expansion mode:

| Metric | Result |
|---|---:|
| Days tested | 120 |
| Trades | 40 |
| Wins / Losses | 28 / 12 |
| Win rate | 70.00% |
| Net summed trade return | 28.15% |
| Portfolio return on $10,000 with $1,000 trades | 2.81% |
| Ending equity | $10,281.49 |
| Max drawdown | -0.72% |
| Average net per trade | 0.70% |
| Max losing streak | 2 |
| Best month | $227.93 |
| Worst month | -$66.25 |
| Estimated monthly profit with allocator | $104.70 |

Final tuned modes:

| Symbol | LOOP Distance | Trades | Win Rate | Est. Monthly Profit per $1k |
|---|---:|---:|---:|---:|
| DOGE/USDT | 1.2% | 12 | 75.00% | $26.14 |
| SOL/USDT | 2.0% | 4 | 100.00% | $24.73 |
| LTC/USDT | 1.0% | 9 | 77.78% | $12.07 |
| ALGO/USDT | 2.0% | 9 | 55.56% | $6.93 |
| ETH/USDT | 2.0% | 6 | 50.00% | $0.50 |

## Notes

- Smaller LOOP distances produced more trades but worse total portfolio returns.
- One-trade winners such as ADA, XRP, LINK, and DOT were kept out of allocation because the sample is too small.
- This is a backtest/optimizer result, not a guarantee of live trading profit.

## Timeframe Comparison

The same 120-day optimizer was run on `30m` and `1h` candles to test whether higher timeframes produced cleaner LOOP setups.

| Timeframe | Allocation Estimate | Best Qualified Symbols | Read |
|---|---:|---|---|
| 15m | $104.70/mo | DOGE, SOL, LTC, ALGO | Best tested setting |
| 30m | $65.85/mo | ALGO, SOL, XRP, DOGE | Lower profit and weaker broad distance results |
| 1h | $57.00/mo | DOGE, SOL | Fewer qualified symbols and worse broad distance results |

Conclusion: keep live Loopbots on `15m` for now. The higher timeframes were useful to test, but they did not improve profitability for the current LOOP strategy.

## Sideways Accumulation Research

A separate `sideways` backtest preset was added to study SOON-style LOOP behavior: sideways range, lower-half entry, volatility, bounce confirmation, and wider LOOP distances.

Broad sideways mode across the full test universe had too many bad symbols, but the targeted best-candidate basket was meaningfully stronger:

| Metric | Current Tuned Trend | Targeted Sideways Basket |
|---|---:|---:|
| Days tested | 120 | 120 |
| Trades | 40 | 159 |
| Win rate | 70.00% | 59.12% |
| Portfolio return | 2.81% | 6.61% |
| Max drawdown | -0.72% | -2.42% |
| Avg net per trade | 0.70% | 0.43% |
| Max losing streak | 2 | 6 |
| Estimated monthly allocator profit | $104.70 | $218.85 |

Targeted sideways candidates:

| Symbol | Best LOOP Distance | Trades | Win Rate | Est. Monthly Profit per $1k |
|---|---:|---:|---:|---:|
| DOGE/USDT | 2.0% | 26 | 69.23% | $59.22 |
| ALGO/USDT | 2.5% | 29 | 58.62% | $44.67 |
| ETH/USDT | 2.5% | 28 | 53.57% | $21.83 |
| BNB/USDT | 1.5% | 23 | 60.87% | $20.15 |
| BCH/USDT | 1.2% | 28 | 60.71% | $14.92 |
| SOL/USDT | 2.5% | 25 | 52.00% | $11.30 |

Conclusion: sideways accumulation is promising as a separate research/live-paper mode, but it is riskier than the current tuned trend strategy. It should be paper-tested before real allocation because it had a larger drawdown and longer losing streak.

## Combined Live Method

The live config now includes both methods:

- `Trend pullback`: the original uptrend + pullback + bounce method.
- `Sideways accumulation`: SOON-style range chop, lower/middle range entry, wider LOOP distance.

The Telegram entry alert includes a `Method:` line so each alert identifies which method fired.

Final combined 120-day backtest:

| Metric | Result |
|---|---:|
| Trades | 168 |
| Wins / Losses | 104 / 64 |
| Win rate | 61.90% |
| Portfolio return on $10,000 with $1,000 trades | 7.95% |
| Ending equity | $10,794.79 |
| Max drawdown | -2.67% |
| Average net per trade | 0.47% |
| Max losing streak | 5 |
| Best month | $537.27 |
| Worst month | -$93.12 |
| Estimated monthly allocator profit | $239.55 |

Live methods by symbol:

| Symbol | Methods |
|---|---|
| DOGE/USDT | Trend pullback + sideways accumulation |
| SOL/USDT | Trend pullback only |
| LTC/USDT | Trend pullback only |
| ALGO/USDT | Trend pullback + sideways accumulation |
| ETH/USDT | Trend pullback + sideways accumulation |
| BNB/USDT | Sideways accumulation only |
| BCH/USDT | Sideways accumulation only |
