# Paper trading stack — Kraken, Binance testnet, Alpaca

What is installed, what each piece does, and what is deliberately not built.
Supersedes the `SETUP.md` that came with the first drop of these files, two of
whose steps were wrong (see "Not applied").

## Installed

| File | What it is |
|---|---|
| `routines/triangular_arb.py` | Read-only intra-venue triangular arbitrage scanner. Places no orders. |
| `routines/alpaca_etf_sweep.py` | Sweeps a share of realized crypto P&L into index ETFs on Alpaca **paper**. |
| `condor/alpaca_paper.py` | Minimal Alpaca client. Refuses any host but the paper one, at construction. |
| `tests/test_triangular_arb.py`, `tests/test_alpaca_paper.py` | Both, against the real modules. |

Routines are auto-discovered; they appear under `/routines` on the next reload.

## Credentials

```
ALPACA_API_KEY_ID=
ALPACA_API_SECRET_KEY=
```

Generate them at app.alpaca.markets under **Paper Trading**; paper keys are
separate from live keys. The commented placeholders are in `.env.example`.

`triangular_arb` needs no keys: the API serves public tickers for a plain
connector name (`kraken`) without credentials.

## Running the two routines

**`triangular_arb`** defaults to `connector=kraken`. A `*_paper_trade` or
`*_testnet` name is accepted and read from its mainnet twin, because the API
excludes those names from its ticker pool (`400`, "excluded from the ticker
pool"); the report says when it did that. Cycles are priced most-liquid first
(24h quote volume of the legs touching `base_currency`), up to `max_cycles`.
Three taker legs at 26 bps cost 78 bps, so expect few or no hits.

**`alpaca_etf_sweep`** defaults to `dry_run=true`: it computes and reports, places
nothing, and does not touch its ledger.

- The ledger is `data/alpaca_etf_sweep_ledger.json`. `swept_pnl_total` only moves
  up, so a drawdown defers the next sweep and never reverses one.
- It advances by what Alpaca **accepted**, not what filled. A partial basket
  failure advances it only by the placed share; the rest stays pending.
- A ledger that exists but cannot be read makes the routine **refuse**, not start
  over: a fresh ledger would sweep all lifetime P&L a second time.
- P&L is the realized total of **active** bots. A stopped-and-removed bot takes
  its P&L with it, and sweeps pause until the remaining total passes the ledger.

## Status by strategy

| Strategy | State |
|---|---|
| Market making | Stock `pmm_*` controllers. The Kraken paper configs are **held back**, below. |
| Triangular arb | `routines/triangular_arb.py`. |
| ETF sweep | `routines/alpaca_etf_sweep.py`. |
| Pairs trading | **Not built.** The API ships no stat-arb controller. |
| Cross-market arb | **Not built.** The stock `arbitrage_controller` and `xemm_multiple_levels` ship with the API, unconfigured. |

### Kraken paper configs: held back, unverified

`pmm_kraken_paper_btc.yml` and `pmm_kraken_paper_eth.yml` target
`kraken_paper_trade`. This repo's own notes say the opposite of what the original
setup note assumed: commit `af6b6e0` records that the `*_paper_trade` connectors
lack trading rules, and the `hft_volume_risk` skill says they do not run
controllers. They also drop `leverage: 1` and the rebalance keys that the shipped
`spot.yml` sets. Neither claim could be tested (the API was down), so the configs
stay out of `sample_configs/`, where an agent would pick them up. Deploy one by hand
once the API is up; if it runs, move it there.

### Cross-market arb is not blocked on scanning

The original note called it blocked because testnet prices are synthetic. That is
true for *executing* against a testnet. It does not stop *scanning* two real
venues' public books, which `routines/arb_check.py` already does.

## Things that will bite you

- **`pmm_simple` is not Avellaneda-Stoikov.** No inventory skew, no volatility
  scaling; it quotes symmetrically around mid and will accumulate one-sided
  inventory in a trend. A-S behaviour is `pmm_v1` / `pmm_mister` or a custom
  controller.
- **Paper fills are optimistic.** Simulated against the public book with no queue
  position and no competition. Maker P&L depends on queue position; expect live to
  be materially worse.
- **Alpaca's free data is IEX only**: 30 WebSocket symbols, 15-minute history, and
  IEX quotes diverge from the NBBO. Fine for market orders in liquid ETFs, not for
  calibration.
- **The sweep is reallocation, not hedging.** VOO and VTI are positively correlated
  with crypto in risk-off conditions and nothing is sized against live exposure.
- **No API moves cash between the legs.** Alpaca's transfer endpoints are Broker
  API, for registered firms. Live, funding is an ACH you do by hand.

## Not applied

- **`0001-expose-testnet-connectors.patch`.** It was never in the drop, and it would
  have reverted `af6b6e0`: testnets hidden by default behind `?include_testnet=true`,
  and `*_paper_trade` presented as a real trading target. `tests/test_testnet_connectors.py`
  asserts the committed behaviour.
- **`mkdir condor/venues`.** `condor/venues.py` already exists and nine modules import
  it. A package of the same name shadows it and breaks all of them. The Alpaca
  client lives in `condor/alpaca_paper.py` instead.
