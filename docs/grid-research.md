# GRID Bot Research

This is a research lane only. It is not part of the live Telegram LOOP alerts yet.

## What Was Tested

The research backtester approximates Bitsgap-style spot GRID bots using quote-sized USDT orders, based on the screenshots:

| Preset | Approx range around launch | Levels |
| --- | ---: | ---: |
| Short-term | -6.5% / +7.5% | 35 |
| Mid-term | -14% / +17% | 50 |
| Long-term | -25% / +35% | 80 |

Test assumptions:

- 0.1% fee per filled order.
- Grid range is created around the launch price.
- Quote currency order sizing is used.
- Optional take-profit and stop-loss can be simulated on total bot PNL.
- Trailing Up, Pump Protection, and exact Bitsgap internal execution are not fully replicated yet.

Official Bitsgap references used:

- https://bitsgap.com/blog/grid-bot-sideways-how-to-profit-on-market-uncertainty-and-stagnation
- https://bitsgap.com/id/helpdesk/article/10038646989340-Menyesuaikan-Pengaturan-Tingkat-Lanjut-GRID-Bot-Bitsgap

## Current Findings

Fixed GRID tests can look profitable over a favorable recent window, but they fail badly when price trends down through the range.

Recent 120-day fixed 15m examples:

| Pair | Preset | Total PNL | Max DD |
| --- | --- | ---: | ---: |
| ALGO/USDT | Mid | +13.99% | -23.53% |
| DOGE/USDT | Mid | +13.24% | -20.27% |
| LINK/USDT | Mid | +10.42% | -21.24% |

Those are not enough by themselves because the one-year fixed test was negative on BTC, ETH, ALGO, DOGE, and LINK.

## Better Test: Rolling Launches

The better test launches a fresh GRID bot repeatedly, then measures whether the setup works across many possible start dates.

Best recent 120-day setup tested:

- Timeframe: 15m
- Hold window: 14 days
- Launch filter: strict sideways
- Take profit: +5%
- Stop loss: -5%

Top recent results:

| Pair | Preset | Starts | Win rate | Avg 14d | Monthly estimate | P10 return | Worst return |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| DOGE/USDT | Long | 17 | 70.59% | +2.70% | +5.78% | -1.75% | -5.04% |
| DOGE/USDT | Mid | 17 | 76.47% | +2.52% | +5.40% | -1.33% | -5.06% |
| LINK/USDT | Long | 24 | 66.67% | +1.61% | +3.44% | -5.26% | -5.62% |
| BNB/USDT | Long | 21 | 85.71% | +1.50% | +3.21% | -5.02% | -5.17% |
| BTC/USDT | Long | 19 | 68.42% | +1.43% | +3.07% | -2.55% | -5.16% |

The same idea over a 365-day sample did not stay strong. The best profiles were low-return and still had uncomfortable downside. That means this is not proven enough for real size yet.

## Honest Recommendation

Do not turn GRID alerts live with meaningful money yet.

Latest 30-day check on June 4, 2026:

- DOGE no longer looks good in the current short sample.
- BNB long/mid looked best, but only had 2 qualified starts, so the sample is too small to trust.
- Current GRID research priority is manual Bitsgap backtesting on BNB/USDT long and mid presets, not live allocation.

The current best use is research/paper/small-size testing:

- Focus on 14-day GRID windows, not 3-day windows.
- Prefer long or mid presets, not short presets.
- Only consider strict sideways launches.
- Test TP/SL around +5%/-5% and +8%/-3% in Bitsgap's own backtester.
- Keep Trailing Up and Pump Protection on when testing in Bitsgap, but remember this local simulator does not fully model them yet.

For income expectations:

- Recent 120-day filtered GRID math could show roughly 3%-6% monthly on selected bot capital.
- The longer 365-day test does not prove that edge.
- At $5,000 per bot, a proven 1%-2% monthly edge is only $50-$100 per bot per month.
- A 3k-5k monthly target would require either much more capital, a stronger proven edge, or both.

## Commands

Recent strict filtered GRID scan:

```powershell
python run_grid_backtest.py --days 120 --timeframe 15m --investment 1000 --fee-pct 0.1 --history-exchange okx --all-usdt-pairs --max-pairs 15 --rolling --hold-days 14 --step-days 2 --launch-filter strict-sideways --take-profit-pct 5 --stop-loss-pct 5
```

Longer honesty check:

```powershell
python run_grid_backtest.py --days 365 --timeframe 15m --investment 1000 --fee-pct 0.1 --history-exchange okx --symbols BTC/USDT,ETH/USDT,ALGO/USDT,DOGE/USDT,LINK/USDT --rolling --hold-days 14 --step-days 3 --launch-filter strict-sideways
```
