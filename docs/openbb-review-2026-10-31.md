# Equity integration review — October 31, 2026

Review of the working tree while implementation is still in progress. No
implementation files were edited. All reproductions replaced provider calls
with fakes; no external provider access or real paper orders were used.

## Findings to resolve before enabling submissions

1. **P1 — Account access is broader than the existing permission model.**
   `condor/web/routes/paper_equities.py:99` requires only `get_current_user`
   while `_equity_client.py` constructs an install-wide client using the
   environment's Alpaca credentials. Any approved user, including one with no
   permission to any trading server/account, can submit orders and read the
   shared account. The module's claim that settings mutations establish this
   precedent is inaccurate: existing settings routes enforce owner access, and
   trading routes enforce an explicit server grant. Introduce an account-level
   permission check, or restrict the global paper account to admins until
   account grants exist. Apply the same policy to account and position reads.

2. **P1 — Submission bypasses the deterministic preflight policy.**
   `paper_equities.py:114` calls `submit_notional_order()` directly. It never
   retrieves a quote, checks quote age, checks the market/session, reads account
   state/open orders, or applies concentration/order-size policy. A mocked
   $100,000,000 order from an ordinary user reached submission with **zero**
   quote/account/clock calls, despite the fake account having zero buying power
   and the fake clock being closed. Broker-side buying-power rejection is not a
   substitute for the application policy in the integration plan. Route both
   preview and submission through one deterministic service and revalidate at
   submission time.

3. **P1 — Invalid quote timestamps are converted into fresh quotes.**
   `condor/web/routes/equities.py:148` catches an invalid/missing timestamp and
   substitutes `time.time()`. Prices/sizes also default to zero. A mocked quote
   with `t="bad-timestamp"` returned HTTP 200 with an age of approximately
   0.004 seconds. This manufactures freshness for data that cannot qualify for
   execution and also makes the quote-age metric misleading. Reject invalid or
   missing timestamps/prices, preserve timezone-aware provider time, and enforce
   the execution freshness policy. The current route test explicitly accepting
   an unexpected shape as a 200 with zero prices should be corrected.

4. **P1 — Ambiguous submissions cannot be reconciled safely.**
   `paper_equities.py:121` tells callers to check positions before retrying.
   An accepted, unfilled order does not appear in positions. The request has no
   client-order/idempotency identifier, no intent is durably stored before the
   provider call, and there is no order lookup/cancellation API yet. A fake
   provider that accepted an order and then timed out produced: first response
   502, positions `[]`, retry 200, **two accepted orders**. The generic
   `AlpacaAPIError` catch also labels HTTP 5xx outcomes `equity_order_rejected`;
   the same reproduction with a 500 produced that misleading code and a second
   accepted order. Persist a stable client-order ID before submission, reconcile
   uncertain outcomes by that ID, and distinguish definitive rejection from
   unknown status. The existing success-only deed is not an order journal.

## Data correctness findings

5. **P2 — Stale cache reads have no maximum age.**
   `condor/equity_market_data.py:121` returns any stale entry without checking
   `_STALE_MAX_AGE`. Age eviction occurs only when writing a successful result,
   so an extended outage can serve old data indefinitely. A seeded 86,400-second
   old instrument was returned after a rate limit despite the 900-second stale
   constant. Check expiration at read time, define whether the stale allowance
   is relative to acquisition or TTL expiry, and return retrieval/cache-age
   provenance so research screens can label the fallback.

6. **P2 — The empty-instrument exception uses the wrong constructor.**
   `equity_market_data.py:233` calls `YFTickerMissingError(symbol)` but installed
   yfinance 1.7.0 requires `(ticker, rationale, possibly_delisted=True)`. Mocking
   `Ticker.info` to `{}` raises `TypeError`, which becomes `EquityDataError` and
   HTTP 502 instead of the intended not-found response. Supply the rationale
   and exercise the actual sync-wrapper empty-info branch in a regression test;
   existing tests replace that wrapper and miss this path.

## Validation and remaining scope

The selected backend checks passed: **194 tests** across market data, research
routes, paper routes, rollout gates, Alpaca transport/client behavior, server
authorization structure, and deeds. Passing checks currently do not cover the
failure scenarios above. All runtime reproductions used fake provider edges.

At this snapshot the research implementation imports **yfinance directly**, not
OpenBB. No OpenBB adapter/provider configuration or equity frontend workspace
was present. Preview, order listing/cancellation/reconciliation, complete data
provenance and adjustment contracts, and bounded research-provider deadlines
remain unfinished parts of the original plan. These are scope/status notes,
separate from the confirmed findings above.

The existing paper-host allowlist, redirect refusal, and disabled-by-default
submission flags remain present. Keep submission disabled until the P1 items
and paper lifecycle acceptance tests are resolved.
