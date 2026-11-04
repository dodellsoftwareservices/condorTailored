# Simulated Kraken spot exchange

Live public Kraken prices and order books, simulated fills and costs. **Nothing here can
place a real order**: the only network client (`kraken_public.py`) is pinned to
`https://api.kraken.com/0/public/*`, issues GET only, refuses redirects and never sees an API key.

## Use

- Web API (admin only): `/api/v1/sim/kraken/{snapshot,orders,portfolio,costs,advance,config,reset}`
- Agent tools: `sim_market_snapshot`, `sim_submit_order`, `sim_cancel_order`, `sim_list_orders`,
  `sim_portfolio`, `sim_cost_report` (mounted by the `sim_crypto_trader` agent)
- State: `data/sim_exchange/kraken_default.json` (one account, atomic writes, single writer)
- Owner controls (not exposed to the agent): `PUT /sim/kraken/config` for risk limits and
  running costs, `POST /sim/kraken/reset` to start over (the old file is archived as `.bak`)

## How faithful it is

| Aspect | Model | Bias |
|---|---|---|
| Prices, spread, depth | The real live book and ticker | none |
| Market orders | Walk the real book level by level after a sampled network delay, then fetch the book | none known; ignores our own impact on the book |
| Limit orders | Rest with a queue position; filled only by real trades at or through the price | Slightly pessimistic: other participants' cancellations never shorten the queue |
| Fees | `kraken_fees.json` by rolling 30-day volume, charged in quote currency, maker vs taker by how the fill happened | Tiers 3 and up are unverified (see the file) |
| Tick, lot, minimums | Exactly as Kraken reports for the pair | none |
| Latency | Lognormal around 150 ms (20 ms to 2 s) | An assumption: tune `latency_ms` |
| Costs | Fees, slippage vs arrival mid, LLM requests (from the budget ledger), fixed daily cost | LLM and fixed cost default to $0 |

## Not modelled

Margin and shorting, staking, non-USD quote currencies, withdrawals and deposits, hidden or
iceberg liquidity, market impact beyond visible depth, order-book changes caused by our own
orders, and exchange outages or rate limits. The simulator has never been checked against real
executions, so treat its fills as realistic, not as proven.

## Desks, funding and the fee what-if

- Each desk is its own account file and starts with **$50,000** and a **$2,500** per-order cap:
  spot trader (`kraken_default.json`, Portfolio tab `krakenSimHodl`), market maker
  (`kraken_mm.json`, tab `krakenSimMM`), and the futures account (`kraken_futures.json`).
- Owner-only `fee_override` on `PUT /sim/kraken/config`, `/mm/config`, `/fut/config`:
  `{"maker":"0.25","taker":"0.40"}` (percent), `{"volume_usd":"10000000"}` (spot only: price as
  if that 30-day tier were reached) or `{}` to clear. Never exposed to an agent tool.
- **Futures** (`/sim/kraken/fut/*`, tools `sim_fut_*`, agent `sim_futures_trader`): USD-margined
  perpetuals from Kraken Futures' public endpoints, with shorting, funding accrual, a maintenance
  margin liquidation and 2 bps maker / 5 bps taker fees. See `futures.py` for what is modelled.
- The maker records `mm_autostart.json` while running and resumes after a restart. Stop and a
  loss-limit halt delete it. Condor auto-reloads on file changes, which restarts the process.
