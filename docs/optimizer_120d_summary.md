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
