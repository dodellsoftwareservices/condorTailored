# Portfolio dashboard — November 3, 2026

Home (`/`) is now a dashboard hub. The agent conversation workspace is available
at `/agents`; existing links carrying `agent`, `ask`, `conversation`, `panel`, or
`desk` still reach it with their parameters intact. The dashboard supports paper
account selection, separate live balances, history ranges, manual refresh and
15-second, 30-second, one-minute, or disabled automatic refresh.

## Account basis

The operator confirmed **$100,000 initial capital for each of Alpaca Paper and
KrakenSim**, with **$2,500 maximum order notional on both**. Total invested for
these two paper accounts is therefore $200,000. Order caps do not reduce the
capital baseline. Buying power is not counted as equity.

The existing KrakenSim account started at $10,000. During rollout its starting
cash and cash balance were increased by the same $90,000 difference, preserving
its holdings, orders, fills, fees, and PnL. The account was backed up privately
before the change, and Condor was stopped during the write. New simulator
accounts and default reset requests now start at $100,000. No orders or resets
were submitted to validate the dashboard. Alpaca's environment order cap remains
$2,500; the environment and API credentials were not rewritten.

## Metric definitions

| Metric | Basis |
| --- | --- |
| Total invested | Original paper-account funding; each account's confirmed starting equity. |
| Portfolio value | Cash plus current marked holdings. Excludes leveraged buying power. |
| Net PnL | Current equity minus starting capital, since initial funding. Exchange execution costs are included in equity. Simulator fixed and model operating costs remain in its separate cost report. |
| ROI | Net PnL / starting capital, since funding; not annualized. |
| Batting average | Winning matched closing sell orders / all matched closing sell orders in the selected range. Flat outcomes count in the denominator; partial fills of one order count once. |
| Sharpe ratio | Mean daily return / sample standard deviation of daily returns, using a 0% risk-free rate. Daily, not annualized; at least seven daily returns and nonzero variance are required. |
| Maximum drawdown | Greatest observed decline from an equity peak in the selected range. This does not reconstruct unobserved prices. |
| Trading volume | Buy plus sell fill notionals over the available lifetime execution history. Incomplete or unmatchable history is withheld. |
| Capital deployed | Gross marked position exposure, separate from cash and initial funding. |

Alpaca supplies account equity, positions, portfolio history, and paginated fill
activities. Long-side outcomes are matched with FIFO entry costs. Shorts or
missing entry lots make the outcome population unavailable. Broker corporate
actions and non-trade adjustments are not reconstructed into individual trade
outcomes; account-level equity remains the PnL source. Alpaca outcome matching
does not allocate separate regulatory adjustments to each closing order.

KrakenSim supplies its recorded realized closing outcomes and marked portfolio.
The dashboard reads a dedicated observation method that does **not** advance or
match resting orders. A five-minute background job records simulation equity
even with the browser closed. Its durable history retains up to 27,000
five-minute observations plus the known initial cash point, with a new history
epoch when the simulator is reset. Historical prices and trades are not invented.

Combined time series use observations available for both accounts and do not
backfill unobserved simulator history with guessed prices. Therefore combined
history may be shorter than Alpaca's history; choose Alpaca alone for its older
records. Simulated and live money are never combined. Live account starting
capital and execution statistics are not inferred from current balances.

## Availability and permissions

`GET /api/v1/dashboard/paper?range=1M` retains the existing shared paper-account
admin restriction. Reads have a 12-second deadline per source, a 25-second cache,
and shared concurrent fetches. Failure to load one source preserves independently
available observations; an unavailable account never becomes a zero-dollar
account or a falsely complete combined total. Live exchange balances continue
through existing server-access checks.

Metric descriptions and data sufficiency are visible in the interface. Paper
funding references remain separate from market price history and per-order risk
limits. New funding or account resets require the capital reference to be reviewed.

References: [Alpaca portfolio history](https://docs.alpaca.markets/us/reference/getaccountportfoliohistory-1),
[Alpaca fill activities](https://docs.alpaca.markets/us/reference/getaccountactivitiesbyactivitytype-1),
and [Sharpe's definition](https://web.stanford.edu/~wfsharpe/art/sr/sr.htm).

## Validation and rollout

- Full frontend suite: **2,509 passed across 248 files**.
- Focused dashboard, simulator, Alpaca transport/account, permission, and
  authenticated-documentation coverage: **164 passed**.
- Production build and lint baseline checks passed. The existing large-bundle
  warning and 41 baselined lint errors remain.
- Browser checks exercised account selection, history ranges, chart measure
  selection, disabled auto refresh, desktop/mobile layout, both themes, and the
  Agents navigation. No browser runtime errors or page-wide horizontal overflow
  were observed.
- The live paper endpoint returned both accounts online with $200,000 combined
  starting capital and $2,500 per-order caps. Portfolio values were read from the
  actual Alpaca account and the persisted simulator, not UI fixtures.
- Condor was rebuilt and restarted. The five-minute equity sampling job is
  registered, and the dashboard is deployed at the home route.
- Configuration, environment, audit, and agent-setting files retained their
  pre-work hashes. Simulator funding was the intentional state change described
  above. No real or simulated orders were submitted for verification.
