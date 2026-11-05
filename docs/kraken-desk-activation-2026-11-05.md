# Kraken desk inactivity review — November 5, 2026

## Findings

- The futures account existed, but `sim_futures_trader` had no sessions or run
  history. Its zero fills represented a desk that had never been started.
- The spot desk completed 32 reviews in the eight-hour shift. Every review
  chose cash, primarily using 12 short intraday candles, a nearby VWAP target,
  and a spot round-trip fee hurdle. This was an intraday mandate attached to
  an account presented as long holds.
- The shift selected DeepSeek explicitly, but the saved spot and futures
  strategy defaults still named the old free Nvidia model. A later spot
  session inherited that model again.
- Both public market-data feeds work: daily spot history and hourly futures
  history returned valid candles. Futures was not blocked by connectivity.

The current spot snapshot reports 80 bps maker / 160 bps taker round trips;
the futures snapshot reports 4 / 10 bps, before spread, impact and funding.
The spot entry-tier fees agree with [Kraken's published schedule](https://www.kraken.com/features/fee-schedule).

## Changes applied

- Source defaults and saved runtime configs now select
  `openrouter:deepseek/deepseek-v4.1-flash-1017`. The existing runtime provider
  allowlist pins that model to `wafer/mxfp4`.
- Long holds now review hourly over a 3–30 day horizon using daily and 4-hour
  data. Entries need a defined trend/reclaim or reversal setup, target,
  invalidation, reward/risk and actual execution costs. A 15-minute range is
  no longer used as the ceiling for a multi-day target.
- Futures now runs every 15 minutes, evaluating both long and short candidates
  with hourly and intraday history, plus costs and funding. Exits must be
  `reduce_only`.
- Both desks must journal a checkable candidate screen and missing trigger
  when flat, rather than repeating a generic no-edge statement.
- Initial entries are limited by instructions to $500 per leg. Owner-enforced
  per-order caps remain $2,500. Spot retains its 5% gross-exposure cap; futures
  was tightened to 0.05x gross exposure, approximately $2,500 on $50,000, with
  a $2,500 per-contract cap and two open-order slots.
- Both saved configurations retain `restart_on_boot: true`. Portfolio account
  groups now expose whether the desk is running and its actual review cadence.

## Live verification

- Spot session `sim_crypto_trader.kraken_sim_paper_14` completed its first
  review at 20:01 UTC (3:01 p.m. EST), using daily and 4-hour snapshots for all
  three pairs. It recorded specific reversal triggers and remained flat.
- Futures session `sim_futures_trader.kraken_futures_paper_1` completed its first
  review at 20:03 UTC (3:03 p.m. EST). Its portfolio, orders, costs and market
  tools completed successfully. It recorded breakout/breakdown and relative-value
  candidates, rejected the current setups, and remained flat.
- Both sessions pin DeepSeek in their durable `status.json` and have successful
  provider responses in the request ledger.

No trade was forced to manufacture activity. These changes establish two
working, distinct desks; they do not establish that either strategy has an edge.
