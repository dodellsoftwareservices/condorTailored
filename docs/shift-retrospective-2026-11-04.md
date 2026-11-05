# Eight-hour shift retrospective — November 4–5, 2026

## Outcome

The requested supervised eight-hour shift **did not start**. No shift profile was
created, and `condor/data/shifts` contains no shift record, runner log, or final
report. The shift runner made no testnet trades. Preparation stopped after
checking the running Condor and Hummingbot services, the registered
`binance_perpetual_testnet` credential, and its paper balance.

This is a retrospective of activity that was already running independently. It
is not a complete event log for a supervised shift.

## Intended window

Approximately **November 4, 4:24 p.m. to November 5, 12:24 a.m. EST**
(21:24–05:24 UTC). The start time follows the local clock observed while the
shift was being prepared; no shift record exists to establish an exact start.

## Independent simulator activity in that window

| Account or process | Observed activity | Result |
| --- | --- | --- |
| Kraken spot agent, session 8 | 29 journal ticks (#5–#33); all record `actions=0`. Eight consecutive ticks (#19–#26) failed to parse the model response. | No orders or fills; account remained in cash. |
| Kraken spot simulated account | No fills, holdings, orders, fees, slippage, realized P&L, or volume in its persisted ledger. | $50,000 starting cash and $50,000 current cash; $0 trading P&L. |
| Kraken futures simulated account | No positions, orders, fills, fees, funding paid, or liquidations in its persisted ledger. | $50,000 starting cash and $50,000 current cash; $0 trading P&L. |
| Kraken simulated market maker | 934 limit orders created during the window; all 934 are recorded as cancelled. No fills in its persisted ledger. | $50,000 cash; $0 fees and realized P&L. |

The spot agent's eight failed ticks reported `Invalid response from openai chat
completions endpoint` because `choices` was `null`; normal HOLD entries resumed
at tick #27. Its journal attributed the lack of trades to BTC, ETH, and SOL
ranges that did not clear its fee-aware edge threshold. No trade was placed to
test execution.

## State when checked on November 5 around 2:55 a.m. EST

- The Condor HTTP health endpoint and Hummingbot API answered; Hummingbot API,
  EMQX, and Postgres containers were running.
- Kraken spot agent session 8 was marked `running`, at tick #42, last updated
  around 2:43 a.m. EST. It was not bounded by this requested shift.
- The simulator balances above were read from the persisted JSON ledgers after
  the intended window. Since those ledgers have no fills, fees, or funding, the
  cash and trading-P&L conclusion also applies to the window.

## Evidence

- `.condor/agents/sim_crypto_trader/loops/kraken_sim_paper/sessions/session_8/journal.md`
- `.condor/agents/sim_crypto_trader/loops/kraken_sim_paper/sessions/session_8/status.json`
- `data/sim_exchange/kraken_default.json`
- `data/sim_exchange/kraken_futures.json`
- `data/sim_exchange/kraken_mm.json`

The journal records tick outcomes rather than every model request or public
market-data response. There is no shift-level audit trail for the intended
window.
