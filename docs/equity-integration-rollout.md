# OpenBB / Alpaca Paper Markets: rollout and acceptance

This is the bottom-up delivery track for the integration plan. The adapter,
canonical data models, equity API handlers, and trading workspace are a separate
implementation track. No provider capability, entitlement, or release phase is
approved merely because these controls exist.

## Implemented controls

`condor/web/equity_rollout.py` is installed in `create_app()` and gates the
planned `/api/v1/equities` and `/api/v1/paper/equities` route families:

| Configuration | Research API | Paper POST requests | Paper GET / DELETE requests |
|---|---|---|---|
| Default / both flags false | Disabled | Disabled | Reach route authorization |
| `EQUITY_MARKETS_ENABLED=true` | Enabled | Disabled | Reach route authorization |
| Both flags true | Enabled | Enabled | Reach route authorization |

The second flag is `EQUITY_PAPER_TRADING_ENABLED`. Only the literal value
`true` (case insensitive, surrounding whitespace ignored) enables a flag. A
disabled request returns HTTP 503 with `detail.code=equity_feature_disabled`.
CORS preflight remains available. Gates do not create endpoints or replace
authentication, account ownership checks, freshness validation, or risk checks.

Turning flags off blocks preview and new submissions while preserving account,
position, order reads, and cancellation so existing orders can be reconciled.
These flags govern the new HTTP surface; they do not disable the existing ETF
sweep routine, MCP tools, or future direct service calls. Keep the sweep in
`dry_run=true` independently during acceptance. Change the process environment
and restart the backend to apply deployment configuration changes.

The existing `AlpacaPaperClient` also now:

- Refuses redirects on every request, including data requests carrying keys.
- Rejects unapproved hosts at construction and at the HTTP request boundary.
- Keeps upstream response bodies and transport exception details out of errors.
- Preserves HTTP status codes and distinguishes ambiguous transport failures
  with `AlpacaTransportError`; no automatic retry is introduced.
- Rejects non-finite, nonpositive, and rounded-to-zero notionals and invalid sides
  before any HTTP request.
- Returns provider order status unchanged; an accepted response is not a fill.

## Monitoring

`GET /api/v1/settings/data-providers/equities` requires authentication and a
current admin role. It performs no provider requests and exposes rollout state
plus process-local counters and timing aggregates. No samples means unknown,
not healthy. Counters reset on restart; combine snapshots from each process if
deploying multiple workers. This is local diagnostics, independent of opt-in
usage telemetry, and exports nothing automatically.

The Alpaca HTTP boundary records provider successes, errors, rate limits,
timeouts, latency, order submission attempts, and HTTP order rejections. An
order timeout is an unknown outcome, not a rejection. A 2xx response with a
rejected lifecycle status must be counted by the execution/reconciliation layer.

`condor/equity_observability.py` provides shared, thread-safe recording hooks:

| Event / measurement | Producer |
|---|---|
| `provider_ok`, `provider_error`, `provider_rate_limited`, `provider_timeout` | Alpaca HTTP; OpenBB adapter must record its own observations |
| `provider_latency_seconds` | Alpaca HTTP; OpenBB adapter must time its own calls |
| `order_submission` | Alpaca HTTP POST attempts, not accepted/filled orders |
| `order_rejected` | Alpaca HTTP 4xx on submissions; reconciliation must also record rejected statuses |
| `cache_hit`, `cache_miss` | Reserved for shared cache implementation |
| `quote_age_seconds`, `stale_quote_rejected` | Reserved for quote validation / execution service |
| `ws_reconnect` | Reserved for equity socket lifecycle |
| `reconciliation_failure` | Reserved for reconciliation service |

Use `equity_metrics.record(EquityEvent.CACHE_HIT)` and
`equity_metrics.observe(EquityMeasurement.QUOTE_AGE_SECONDS, age_seconds)`.
Only the fixed enum names are accepted: never add URLs, symbols, account IDs,
credentials, arbitrary upstream messages, or response payloads as metric labels.
Compute cache hit rate as hits / (hits + misses), and provider failure and
rejection rates over a chosen observation window. A zero denominator is unknown.

Equity HTTP responses carry a generated `X-Correlation-ID`; incoming identifiers
are not trusted. API logs record that ID, method, and status without request
URLs, bodies, or exception details. `equity_correlation_id` is a request-local
context variable propagated into async calls and reset on exit. The Alpaca
client logs its fixed source identity, that ID (or `None` for background work),
sanitized outcome, and latency. OpenBB / execution logging must use that same
context and preserve provenance without exposing secrets.

## Repeatable checks

Ordinary checks require no provider credentials or network access:

```sh
uv run pytest -q tests/test_equity_observability.py \
  tests/test_equity_rollout.py tests/test_alpaca_paper_transport.py \
  tests/test_alpaca_paper.py tests/test_alpaca_paper_smoke.py
```

The external smoke test skips by default. To run it intentionally, inject
`ALPACA_API_KEY_ID` and `ALPACA_API_SECRET_KEY` into the process using your
normal server-side secret mechanism, then:

```sh
RUN_ALPACA_PAPER_SMOKE=1 uv run pytest -q tests/test_alpaca_paper_smoke.py
```

The smoke test reads account, positions, market clock, and an AAPL quote. It
never submits or cancels an order, never prints credentials, and does not load
`.env` itself. It checks connectivity and response structure, not execution
freshness, exchange coverage, entitlement, or realistic fills. Do not enable it
in ordinary CI. A full paper order lifecycle remains a separate acceptance
check once the deterministic execution service exists.

## Remaining acceptance gates, from last phase to first

| Phase | Evidence required before marking complete |
|---|---|
| 5. Limited rollout | Both switches explicitly enabled only after prior gates; monitor stale rejects, failures, and reconciliation. Exercise disabling new submissions with orders still open. Paper-only user acceptance remains pending. |
| 4. Portfolio / streaming | Account-isolated updates; REST reconciliation after reconnect; duplicate and out-of-order event handling; visible valuation timestamps; no delayed polling described as streaming. |
| 3. Paper execution | Fresh quote revalidation on submission, account/open-order exposure checks, deterministic limits, durable client-order ID journal, idempotent concurrent submissions, timeout lookup before retry, rejected/partial/canceled lifecycle tests. |
| 2. Market UI | Symbol/account-isolated query keys; rapid symbol changes; chart series identity; stale/entitlement/closed-market states; preview → review → real receipt; keyboard/mobile acceptance. |
| 1. Read-only data | Fixture-driven provider normalization; UTC and decimal serialization; adjustment modes; cache TTL/coalescing/timeouts; explicit fallback provenance; provider health observations. |
| 0. Data spike | Selected provider capability matrix, actual timestamps / feeds, entitlement and redistribution review, approved fallback policy. No provider is selected by this rollout work. |

The gates above are deliberately pending where their producer does not exist.
Do not infer that zero recorded stale submissions or reconciliation failures is
an exit criterion when quote validation or reconciliation is not instrumented.
This integration delegates fills to Alpaca paper; it is not an NYSE order-book
simulator.
