# Kraken simulator calibration audit — 2026-11-04

The owner kept a fresh $50,000 simulated account after the reset. This report
records the earlier $100,000 account without adding its loss to the new account.
The original state is archived locally at
`data/sim_exchange/kraken_default.1793823212.bak`.

## What happened

The agent's old playbook required calibration round trips even when no trading
edge cleared fees. It bought BTC twice and later sold both positions. The
portfolio looked empty because the account was flat after the sells. Three
partial fills of the second buy made the dashboard show six fills for four
executed orders.

| Measure | Archived result |
| --- | ---: |
| Starting cash | $100,000.00 |
| Ending cash and equity | $99,995.66082 |
| Net account P&L | −$4.33918 |
| Exchange fees | $3.99844 |
| Price difference across the two round trips | −$0.34074 |
| Traded notional, both sides | $999.61138 |
| Executed orders / fills | 4 / 6 |
| Positions / open orders at archive | 0 / 0 |

All four orders had `cal-` client order IDs. These are simulated fills and
fees; no real Kraken account or funds were used.

| Filled (UTC) | Side | BTC quantity | Notional | Fee | Closed P&L |
| --- | --- | ---: | ---: | ---: | ---: |
| 18:00:37 | Buy | 0.00310756 | $249.99761 | $0.99999 | — |
| 18:51:29 | Sell | 0.00310756 | $249.69618 | $0.99878 | −$2.30020 |
| 19:24:43 | Buy, 3 partial fills | 0.00312000 | $249.97845 | $0.99991 | — |
| 19:41:58 | Sell | 0.00312000 | $249.93914 | $0.99976 | −$2.03898 |

The closed P&L on each sell includes both its buy and sell fees. Do not add
the fee column to the closed P&L again.

## Corrections

- Removed the calibration mandate from the agent brain and its loop playbook.
- Added a backend rejection for new buy orders tagged `cal-`.
- Added persistent Kraken trade-ID replay protection before matching resting
  orders. The old fill records did not store source trade IDs, so this guard
  cannot prove whether the two tiny fills at the same timestamp were distinct
  Kraken prints. The archived balance was not altered.
- Added executed-order history to the cost report and portfolio tab. It groups
  partial fills, shows fees, and labels historical calibration orders.
- Restarted Condor to load the backend changes. The new $50,000 HODL account
  remained flat after its first tick; the market maker resumed on its separate
  $50,000 simulated account.

## Verification

- Simulator engine, service, and market-maker tests: 53 passed.
- Full frontend suite: 249 files and 2,513 tests passed; lint and build passed.
- Live `GET /api/v1/sim/kraken/costs` returned the new executed-order field
  after restart. The current account has no historical orders because it was
  intentionally reset; the archived run above retains the prior evidence.
- A live calibration-tagged buy request for an invalid pair returned
  `sim_rejected` before market lookup; fees and fill counts remained zero.
