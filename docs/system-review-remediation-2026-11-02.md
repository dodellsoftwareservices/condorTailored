# Verification report remediation — November 2, 2026

Follow-up to [the verification report](system-review-verification-2026-11-02.md).
The nine findings have source changes and regression coverage. The live rollout
and subsequent fixes are recorded below; exchange registrations and live
credentials are left to the operator's connection work.

## Changes

| Verification finding | Implemented change |
|---|---|
| 1. TOTP secret exposed through preferences | GET and PATCH preferences exclude the authenticator secret and recovery hashes. Enrollment is the only API returning the newly generated secret. |
| 2. Telegram 2FA login consumes its challenge too early | Pending tokens can be inspected without consumption. Missing or mistyped codes keep the challenge pending; successful proof consumes it atomically before the handler yields. TTL still applies. Per-challenge throttling supplements source-IP throttling. |
| 3. Admin recovery bypasses the caller's own 2FA | Recovery refuses all admin targets, matching the existing UI and documented policy. Admins use the factor-protected Security flow for their own account. Ordinary-user recovery remains available to admins. |
| 4. Session refresh and logout leave independent credentials alive | Login sets the signed credential only in the HttpOnly cookie. The response's legacy `token` field now contains a non-secret session identifier. Session reads return that same identifier without creating credentials or extending expiry. SQLite-backed revocations survive restarts and are checked across processes. Browser WebSockets use the cookie, check Origin, and close after revocation/expiry. |
| 5. Tests overwrite install configuration | ConfigManager resolves absolute paths once and keeps its audit file beside its config. `CONDOR_CONFIG_PATH` supports isolated storage. Pytest isolates collection-time access before tests import, then gives each test a separate singleton/config/audit location. A subprocess regression verifies that even collection-time writes leave sentinel install files unchanged. |
| 6. Routine failures report success | The cited scanner branches and additional missing-backend/error returns use `RoutineResult(failed=True)`. The continuous price monitor requests strict price errors and reports outages/recovery while still running. Service health aggregates notices, escalates on the third failed check, reminds after 15 minutes, and reports recovery. A periodic, read-only backend authentication probe runs every minute, beginning 20 seconds after startup. |
| 7. Cost admission waits for an entire run | Each direct-model response records its priced cost before another model/tool step is admitted. With a dollar cap, a shared file lock serializes admission across processes using the same ledger. Unknown pricing or ambiguous interrupted requests pause subsequent admission until UTC reset. Known priced subtotals remain recorded. Response output is capped at 4096 tokens when a dollar cap is enabled. Existing chats observe newly enabled limits; ACP subscription clients are refused rather than silently bypassing dollar controls. |
| 8. Postgres publishes a port on which nothing listens | Compose maps loopback host 5433 to container 5432. The source-mode default, env template and service documentation use host 5433. Container-to-container traffic continues using `postgres:5432`. |
| 9. Yahoo timeout does not enforce its stated bound | Session requests clamp omitted, explicit, tuple and `None` timeouts to at most 20 seconds per connect/read component. The complete research operation has a 60-second caller deadline and a dedicated four-worker pool with bounded admission. Timed-out workers retain their slot until they actually finish, preserving the shared executor's availability. |

Also removed WebSocket URL-token authentication, corrected report-auth comments
and retention-environment documentation, and reconciled the six frontend test
failures. The chart double now creates distinct series so volume writes do not
count as candlestick redraws. Chart display preferences use the common storage
declaration. The detached-specialist fixture now identifies its specialist by
slug, consistent with the separate Griffin identity tests.

Cookie WebSockets also keep the browser's host in development; Vite now proxies
API WebSocket upgrades. This preserves cookie authentication when the dashboard
is opened through `127.0.0.1` rather than `localhost`. Health notification sinks
have a two-second delivery deadline, and failed/cancelled backend acquisitions
close their clients without letting cleanup errors hide the original outcome.

## Operator settings and limits

No dollar amount was selected on the operator's behalf. To opt in, set
`budget.enabled` and a positive `budget.dailyCostCapUsd` in `agent-config.json`.
The JSON schema documents its behavior. `appliesTo` still scopes OpenRouter
request counting; the dollar threshold applies to all direct model providers.
ACP clients such as Claude Code, Codex and Gemini subscriptions cannot be
accurately metered by this ledger and are unavailable while that threshold is
enabled, including on the next turn of an already-running client.

This is a spend threshold based on response pricing, not a provider-enforced
prepaid balance: a response can cross it, and SDK retries, missing final usage,
or provider billing differences can make actual charges uncertain. Uncertainty
pauses future admission. The configured output ceiling and serialized requests
reduce that exposure; provider-side billing limits remain the hard external
boundary. No live model prompts were used for these tests.

Conversation age retention remains opt-in through
`CONDOR_CONVERSATION_RETENTION_DAYS`; unset/zero preserves conversations. Editing
the `.env` file requires a process restart to reload the environment. No retention
period was enabled and no conversations were deleted.

## Rollout

Restart Condor after coordinating with active dashboard users. Existing dashboard
sessions require one new login: JWT versioning retires old credentials, including
the independent refresh tokens issued by the previous implementation. Refresh
the browser so the cookie-based WebSocket client and backend deploy together.
Local OTP/recovery codes now travel in a JSON body instead of the URL.

The Postgres port correction requires recreating that container with the updated
Compose definition; do not remove its data volume. The Hummingbot API's internal
database address does not change. If running the API directly on the host, check
any explicit `DATABASE_URL` override: it should use host port 5433 when connecting
to this container. The existing `.env` was not rewritten.

This pass does not reconstruct the missing server registration or recover the
pre-audit configuration bytes. That incident and the retained running-state
configuration remain documented in the verification report. Configuration,
backup, and credential files were privately snapshotted before this work; tests
are also run in a disposable checkout.

## Validation

- Full backend suite in the disposable checkout: **8,520 passed, 6 skipped**.
  After the final notification-deadline/client-cleanup refinement, **45 focused
  health, isolation, auth and failure-dedup regressions passed** against the final
  source. This includes failures while cleaning up an unreachable backend and
  cancellation during connection acquisition.
- Full frontend suite: **2,486 passed**, across **243 files**.
- Frontend production build: **passed**, with the existing large-chunk warning.
- Frontend lint baseline check: **passed**; 41 pre-existing baselined errors
  remain, with no new lint errors.
- Black/isort checks on the 40 Python files changed in this pass: **passed**.
- Compose validation and Git whitespace checks: **passed**.
- Hashes of the real config, rotating backup, and credential files match the
  private pre-work snapshot. No test data was written into those files.

No services or containers were restarted in this pass. No real exchange orders,
model prompts, or external messages were sent. Source changes remain uncommitted
alongside the existing work; the audit report is preserved with a link here.

## Remaining work outside these nine fixes

- Configure and verify the intended exchange/server connections and budgets.
- Choose conversation retention and infrastructure resource/log limits for the
  actual deployment; neither was silently enabled.
- Resolve the explicitly retained Binance-only research dependencies where an
  alternative feed has suitable history and coverage.
- Docker socket/root access, self-upgrade image trust, and powerful code execution
  remain architectural trust decisions; these fixes do not sandbox them.
- Routine library presentation and optional UI consistency work remain separate
  cleanup items. The testnet registration script is now imported by the venue
  setup helper and should be retained. Large frontend bundles remain a build
  warning.

## Live rollout and connector setup follow-up

Later on November 2, the operator authorized restarting the project and
recreating Postgres. Condor was rebuilt and restarted; Postgres, the broker,
and the Hummingbot API containers were recreated. Postgres retains its original
`hummingbot-api_postgres-data` volume and now publishes loopback host 5433 to
container 5432. A private database dump and configuration snapshot were taken
before the restart. Both database and broker report healthy.

The reported Lighter testnet setup failure is no longer reproducible on the
restarted stack: authenticated requests to the direct API and Condor's connector
configuration route return HTTP 200 and all four testnet fields (L1 address, API
key index, API private key, and account limit). The direct mainnet schema also
returns HTTP 200. No exchange credentials or orders were submitted to validate
this; actual exchange authentication remains to be checked with the operator's
keys.

The Keys and Wallets form also conflated a failed schema request with a connector
that requires no fields, allowing an empty credential submission. It now shows
the schema request error and a retry action, hides credential submission while
loading or failed, and guards the mutation against missing or failed schemas.
Successfully loaded, empty schemas remain supported. Starting a new connector
setup clears stale credential submission errors.

Validation for this follow-up: all 15 Keys and Wallets tests across four files
passed, including new loading, failure/retry, and empty-schema regressions. The
frontend production build and lint baseline check passed. The updated frontend
was deployed, and live API/schema checks passed after rollout.

## Further security and Gateway diagnostics

The original review's unauthenticated API-documentation exposure is closed in
both applications. Swagger UI, ReDoc, the OAuth redirect helper, and
`/openapi.json` require their application's existing authentication: the Condor
session or the Hummingbot API's HTTP Basic credentials. Their responses use
`Cache-Control: private, no-store`. Reverse-proxy prefixes are preserved in the
schema links and the schema's server URL, including Swagger's request execution,
without copying one request's prefix into the shared schema cache. Documentation
requests honor session revocation and the existing user-role checks.

A separate Gateway status defect was fixed: failed API requests, connection
acquisition failures, and malformed status responses no longer return a
successful `running: false` response. Condor returns a sanitized error and resets
its observed running-state transition so recovery still invalidates venue data.
The Gateway tab shows an unavailable state with retry, hides container controls
until status is known, and does not display stale running information after a
failed poll. The wallet setup tooltip also distinguishes unknown status from a
confirmed stopped Gateway.

The API's existing Gateway availability guard was verified, rather than
reimplemented: with Gateway stopped, network discovery returns HTTP 503 with
`Gateway service is not available`. Container status itself correctly returns
HTTP 200 and `running: false` in that case. This separates an absent Gateway from
a failure to reach the API server.

Validation: **70 Condor backend tests**, **16 Hummingbot API tests**, and **18
frontend tests** passed. The production build, lint baseline, Python formatting,
and whitespace checks passed. Operator config, audit, environment, and agent
settings hashes were unchanged by testing. The actual API image also passed an
offline smoke test with test-only credentials and networking disabled.

The API update uses the existing local image and dependencies; the previous image
is retained as `hummingbot-api:before-docs-auth-20261102`. Both applications were
restarted for this follow-up. Live checks confirmed unauthenticated documentation
requests return 401 and authenticated requests return 200 for all four discovery
routes in both apps. Lighter testnet still returns all four fields through
Condor. No exchange credential writes, orders, model prompts, or external
messages were used for these checks.

## November 3 custom-provider probe hardening

The admin gate on custom provider creation already closes the original
non-admin internal-host probe. The `/models` validation request had a separate
gap: aiohttp followed redirects after the URL check, and a hostname could
resolve to a forbidden link-local address when the connection was made. Model
discovery now refuses redirects and checks the connector's actual DNS answers
before opening a socket. It also rejects embedded URL credentials, queries,
and fragments, and revalidates saved URLs when their model list is refreshed.
Local Ollama/vLLM endpoints remain supported unless the operator enables
`CUSTOM_LLM_BLOCK_PRIVATE_URLS`.

This protects the model-discovery probe; it does not establish a network
sandbox for a configured provider used during an actual model run. Admins can
still intentionally configure private endpoints, and the platform's Docker
socket, code execution, and self-upgrade trust boundaries remain as described
above. The focused custom-provider suite passed **53 tests**, including local
redirect and DNS-answer regressions; formatting and whitespace checks passed.
Condor was restarted with the change; its auth-mode endpoint returned HTTP 200,
and the local Hummingbot API connection and dashboard equity sampler resumed.
