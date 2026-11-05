# Verification of the October 31 system review — November 2, 2026

> Follow-up implementation: [November 2 remediation](system-review-remediation-2026-11-02.md).
> The findings below preserve the audit evidence; consult that follow-up for
> fixes, validation, rollout requirements, and remaining operator choices.

Claude made substantial progress on November 1, but the conclusion that only
low-severity security work remains is not supported by the implementation.
Several fixes work individually while leaving another route around the same
control. There are also reliability and cost gaps beyond the security backlog.

This is a review of the working tree, not a security certification. The original
review remains a historical snapshot. Finding numbers below are independent of
its two different numbering schemes.

## Scope and attribution

Read the original review, the relevant Condor and hummingbot-api diffs, new
tests, installed yfinance implementation, and the relevant November 1 Claude
session summaries. Compared source claims with offline endpoint reproductions,
the test suites, and limited live diagnostics. No trading orders were submitted,
no messages were sent, and no services were restarted during this verification.

Condor HEAD is still `2da84ea`; the November 1 work has not been committed.
The tree contains approximately 300 changed/untracked paths spanning earlier
equities work, the Griffin/UI changes, Hyperliquid removal, and this remediation.
The diff alone cannot establish who authored each change. The session summaries
help establish which fixes Claude claimed, but are not verification evidence by
themselves.

## Important verification incident and current operational state

The repository's test isolation does not cover `ConfigManager` and its relative
`config.yml`/`audit_log.yml` paths. During the initial full backend run, the real
`config.yml` and rotating backup were rewritten. I stopped that run after 2,865
passes. I should have snapshotted those files before running the suite.

I then asked the still-running application to save its retained configuration
through an empty preference update, and synchronized the backup to that saved
state. **This recovered the running application's state, not a byte-for-byte
copy of the pre-test files. The original pre-test bytes were not recovered.**
The pre-test main file was 1,132 bytes; the saved running state is 587 bytes.
Post-incident evidence copies were retained outside the repository in the
temporary `condor-review-config-evidence-*` directory.

The running application itself has **zero registered Hummingbot servers**. Its
server-list API returns an empty list, not an authentication error. Its in-memory
state predates the test run, but this alone does not establish when or how the
server registration was lost. Earlier test contamination is plausible: Claude's
session records repeated full-suite runs followed by a restart on November 1
at approximately 23:06 local time. Do not treat that inference as proven history.

Live checks on November 2:

- Condor runs in local mode, in the `condor` tmux session.
- Hummingbot API, broker, and Postgres containers are running; the latter two
  report healthy.
- Authenticated `GET http://127.0.0.1:8000/accounts/` using hummingbot-api's own
  `.env` credentials returns **200**. No credentials were printed.
- Condor local login and its server-list request both return **200**, with
  **zero servers**.

Consequently, the original review's “password fixed on disk; restart needed”
status cannot be carried forward. Restoring the intended server registration
requires separate recovery work; a restart alone does not recreate it. I did
not invent a replacement trading configuration during an audit.

All subsequent full backend testing used a disposable checkout without the
real `.env`, configuration, or runtime stores.

## Findings requiring follow-up

### 1. High — the preferences API exposes the active TOTP secret

Locations: `condor/web/routes/auth.py:335` and `:368`;
`config_manager.py:1052` and `:1171`.

Enrollment stores `web_2fa_secret` in the same preferences dictionary returned
by both GET preferences and successful PATCH preferences. Reserving these keys
against writes does not filter them from responses. The enrollment endpoint's
claim that no other endpoint returns the secret is false.

**Reproduced with a disposable ordinary user:** enable 2FA, GET preferences using
only that user's session, read the exact active secret, compute a code, then
POST disable. Both requests return 200. A compromised session can therefore
bypass the new re-enrollment/disable proof checks without possessing the phone
or any saved recovery code.

Filter secrets and recovery hashes from every public preference response;
prefer keeping authentication state outside the public preferences map. Add
GET and PATCH response tests with 2FA already enabled.

### 2. High — the normal Telegram-plus-2FA browser login cannot complete

Locations: `condor/web/routes/auth.py:63`, `:80`;
`condor/web/auth.py`'s `redeem_login_token`;
`frontend/src/pages/Login.tsx`'s initial login and OTP submission.

The frontend first submits the Telegram login token without an OTP, waits for
`2fa_required`, and then submits that same token with the entered code. The
backend pops the single-use token before checking whether an OTP is present.

**Reproduced:** first POST returns 401/`2fa_required`; second POST with the real
code returns 401/`Invalid or expired login token`. A mistyped code also burns the
link. Direct route tests that supply the correct OTP on the first POST miss the
browser's two-request contract.

Keep a bounded pending authentication challenge until factor verification
succeeds, with atomic final consumption and rate limiting. Test the actual
two-request flow, retries, expiry, and replay after success.

### 3. High — the admin recovery route bypasses its caller's own 2FA

Location: `condor/web/routes/admin.py:327`.

`DELETE /admin/people/{user_id}/2fa` checks the caller's admin role but does not
restrict the target or require a current factor for a self-reset.

**Reproduced:** an admin with 2FA enabled can DELETE their own reset URL using
only their session; the response is 200 and 2FA is off. Hiding the button for
admin targets in `PersonDetail.tsx` does not protect the backend. Claude's own
later session summary noticed this backend gap while treating the UI restriction
as a product limitation; the gap remains executable.

Reject self-reset through the recovery route or require current-factor proof.
Decide separately whether a different admin may recover an admin account, then
enforce that policy on the server and in the UI.

### 4. High — logout revokes one JWT while session refresh creates independent credentials

Locations: `condor/web/routes/auth.py:185` and `:208`;
`condor/web/auth.py:44` and `:97`.

`GET /auth/session` creates a fresh, unrestricted 24-hour JWT with a new `jti`.
Logout revokes only the cookie's original token. The refresh token is described
as WebSocket-only, but no claim or validation rule makes it so: it also works
when sent as the session cookie on ordinary API requests.

**Reproduced:** obtain the refresh JWT, log out with the original cookie, then
replay the refresh JWT as a cookie. `GET /auth/me` still returns 200. The original
JWT is correctly refused. Revocation also disappears when `_revoked_jti` is
cleared, as on restart, and the original JWT becomes valid again. The comment
that this affects only tokens revoked “a few seconds” before restart is wrong;
it affects every revoked token that has not expired.

Returning an equivalent credential from `/auth/session` also means the
HttpOnly migration does **not** prevent same-origin malicious JavaScript from
obtaining a replayable token. Such a script can fetch that endpoint. Moving the
original token out of localStorage is still useful, but the stronger claim in
the comments is unsupported.

Use a durable server-side session identifier/revocation record shared by all
credentials derived from that login, with an absolute expiry. WebSockets already
support cookie authentication; consider removing the browser's need for a raw
JWT instead of minting independent ones for it.

### 5. High — ordinary backend tests can write the operator's real configuration

Locations: `tests/conftest.py:57`; `config_manager.py:136`, `:173`, `:324`;
`condor/preferences.py:42`; `tests/test_agent_input_modes.py:80`.

The shared fixture relocates runtime/data/agent/report roots and agent-config,
but not the ConfigManager singleton or its persistence paths. Per-file fixtures
sometimes isolate it; others reach the real singleton through preference sync
or effective-server resolution. A relative path can also outlive a temporary
working directory and later resolve into the repository again.

**Independently reproduced in a temporary working directory:** run only
`test_agent_input_modes.py`, with all eight tests passing. The test creates and
writes a root `config.yml` outside the fixture's runtime roots. A write trace
shows `utils.auth._refresh_active_server → get_effective_server →
ConfigManager.instance`, followed by preference writes for the fake test user.
The disposable full suite likewise creates a root config containing test data.

Isolate the singleton and both configuration/audit paths for every test, including
collection-time accesses; assert that real install files remain unchanged.
Until then, run the suite in an isolated checkout. A green suite is not evidence
that the install it ran beside is still healthy.

### 6. Medium — handled routine failures still become successful runs

Locations: `routines/market_scanner.py:397` and `:408`;
`routines/mean_reversion_scanner.py:330`;
`routines/price_monitor.py:174`; `condor/routine_store.py:825`.

The new `RoutineResult.failed` plumbing is correct, but not all error returns
use it. If every market-scanner candle fetch fails, the routine returns the
plain string “Failed to fetch candles for any pair.” Normalization sets
`failed=False`, so telemetry, notifications and hooks take the success path.
The no-valid-analysis branches have the same issue.

**Reproduced** the all-candles-failed branch with mocked upstream reads and
observed `failed=False`. Thus the claim that all such cases were fixed is too
broad. Separately, `price_monitor` catches errors inside its never-ending loop,
so the new completion-time failure deduplication never sees those failures.

Convert remaining operational failure returns to explicit failed results or
exceptions. Add tests at each meaningful stage, including all upstream requests
failing, and define health reporting for continuous routines.

### 7. Medium — the dollar cap is delayed accounting, not an effective per-request spend bound

Locations: `condor/acp/pydantic_ai_client.py:1377`, `:1439`, `:623`, `:703`;
`condor/llm/budget.py:521`; `agent-config.json`.

Cost is folded only when an entire model/tool run exits. Every subsequent model
request inside that run sees the old daily total. Overshoot can therefore be a
whole tool-heavy run, or multiple concurrent runs, rather than one response.
The existing per-run request limit is 100. Accounting also drops a whole run's
priced subtotal if any response cannot be priced, and the admission gate is
wired to OpenRouter rather than all supported paid providers.

**Reproduced ledger behavior:** a $0.50 cap admits three requests while the
recorded cost stays $0, then accepts a $3 end-of-run cost record. This is an
offline illustration of admission timing, not a real bill. The installed
`agent-config.json` does not set a dollar cap at all.

The existing boundary was deliberate and Claude disclosed it in its summary;
it should not be described as a general solution to paid-model spend exposure.
Account after each response, handle unknown pricing explicitly, bound output
and in-flight exposure, and make the provider scope visible to the operator.

### 8. Medium — PostgreSQL's published container port is wrong

Location: `../hummingbot-api/docker-compose.yml:142`.

The changed mapping is `127.0.0.1:5433:5433`. PostgreSQL still listens on 5432:
verified live with `SHOW port` inside the container. This published port cannot
reach PostgreSQL. Container-to-container API access still uses `postgres:5432`,
which explains why the API can work and the container healthcheck can pass.

Use `5433:5432` if avoiding a host-side 5432 conflict, and align the source-mode
database URL accordingly. The current source-mode default still names 5432.
I cannot attribute this port edit specifically to Claude from the shared diff.

### 9. Medium — the claimed 20-second yfinance timeout is not actually enforced

Location: `condor/equity_market_data.py:242`.

`kwargs.setdefault("timeout", 20)` only acts when the keyword is absent.
Installed yfinance already passes explicit timeout values: search and core data
calls commonly supply 30; history defaults to 10. The original review's blanket
claim that these calls have no library timeout was also inaccurate for this
installed version.

**Offline transport-level reproduction:** the wrapper passes an explicit 30
through as 30 and an explicit `None` through unchanged. The latter demonstrates
the helper's behavior, not proof that the current Yahoo path supplies `None`.
There is still no application-wide deadline around the complete synchronous
operation, its multiple requests, or retries.

Clamp explicit timeouts if 20 seconds is the intended per-request maximum,
and distinguish that from an overall operation deadline and bounded worker
concurrency. An asyncio timeout alone does not terminate its worker thread.

## What Claude accomplished, and the remaining boundaries

| Area | Verified implementation | Remaining boundary |
|---|---|---|
| 2FA re-enrollment | Requires the active code/recovery code when already enabled | Preferences leak bypasses that proof; admin self-reset also bypasses it |
| Recovery usability | Admin reset route, saved-codes checkbox, clipboard fallback and visible failure copy | Backend target policy differs from hidden UI; Telegram OTP login is broken |
| Auth throttling | Five failures per key in five minutes, Retry-After, login and factor routes covered | In-memory; does not repair alternate factor bypasses |
| Cookie migration | HttpOnly cookie, in-memory frontend token, no durable localStorage JWT | `/auth/session` gives JavaScript an equivalent replayable credential |
| CSRF | Origin middleware on unsafe HTTP methods, using explicit allowed origins | Origin-less requests are intentionally allowed; this is additional protection, not complete session isolation |
| Custom-provider SSRF | Adding a provider now checks admin before the outbound probe | Private endpoints remain intentionally supported for admins |
| Remote HTTP setup | Setup and default-bind startup warnings added | Still plaintext; explicit WEB_HOST suppresses the startup warning |
| Compose startup | Both Postgres and EMQX use `service_healthy` dependencies | Host Postgres publication is wrong; socket/root architecture remains |
| Binance dependency | Many defaults centralized/switched; market scanner now uses the configured ticker endpoint | Mean-reversion and mcap paths remain Binance-bound; default Lighter scan lacks volume data and fails honestly rather than delivering a scan |
| Routine results | `failed` field reaches execution metadata, telemetry and hooks | Several downstream failure branches remain plain strings |
| Failure notifications | Repeated identical completion errors suppressed per instance; success clears suppression | No aggregate venue-down alarm, escalation, or automatic doctor integration; internal continuous-loop failures bypass it |
| LLM costs | Dollar field, ledger, snapshot and admission comparison implemented | Opt-in and unset here; OpenRouter scope, end-of-run delay, unknown pricing |
| Model history | Last 60 whole turns retained, preserving tool-call/result boundaries | A turn can be large; no token-based context budget |
| Conversation disk | Opt-in daily pruning job, activity timestamp, 200 deletions per sweep, off event loop | Unset by default; archive still grows for retained conversations; “edit .env without restart” comment is inaccurate because os.environ is read, not the file |
| Docs/errors | Griffin onboarding, Security guide, runtime distinction, env template/mode docs, targeted OSError sanitization | Historical review statuses were never updated; broad exception/security claims should not be treated as exhaustive |
| Concurrent UI/equities work | Build passes; equities transport/admin gates and cache/error paths have substantial tests | Full frontend suite remains red; not all this work belongs to the remediation session |

## Validation results and limits

- Focused remediation tests: **276 passed**.
- Full backend suite in the disposable checkout: **8,434 passed, 30 skipped,
  14 failed** initially. All 14 failures were missing copied Git metadata or
  sandbox restrictions on local test HTTP listeners/Docker access. After copying
  Git metadata and granting the needed access, **all 14 passed**. Combined:
  **8,448 passed, 30 skipped**. This is a full run plus a targeted recheck, not
  a claim that one uninterrupted invocation was all green.
- Frontend production build: **passed**, with a large-chunk warning.
- Full frontend suite: **2,478 passed, 6 failed**, across 242 files (239 passing).
  Three failures are chart range assertions: the chart double returns the same
  object for candlestick and volume series, so new volume writes inflate the
  counted draws. They do not by themselves establish a production range bug.
  Two failures flag the new chart preference storage key bypassing the shared
  declaration module. One reflects a tab label changing from its supplied label
  to Griffin. Claude had already reported these same six failures before the
  last cost pass.
- Disposable offline endpoint/transport reproductions confirmed findings 1–4,
  6, and the timing/helper behavior in 7 and 9. The isolated fixture probe
  confirmed finding 5. Live Docker/SQL checks confirmed finding 8.

Reproduction scripts and logs are in `/tmp/condor-review-*`; they contain no real
credentials. The application configuration evidence copies are separate and
private. The historical review's sweeping claims about secret-free Git history,
all tool execution, or every LLM path were not certified by this verification.
In particular, the working diff itself removes a previously committed default
database password, so “no hardcoded secrets anywhere in history” should not be
accepted literally.

## Suggested repair order

1. Recover the intended Condor server configuration and fix test isolation before
   running more tests beside the install. Establish an explicit backup/checkpoint.
2. Close the TOTP read leak and admin self-reset, and test the browser's Telegram
   challenge flow end to end.
3. Give session refresh/logout a shared, durable revocation boundary.
4. Correct the routine failure branches and actual venue-health reporting.
5. Fix the Postgres host mapping, then decide and configure the intended dollar,
   history, and disk limits. Reconcile the six frontend test failures.

The remaining original low-severity work includes legacy WebSocket query tokens,
stale report comments, the intentionally powerful code runner, Swagger exposure
considerations, and routine/testnet clutter. Docker socket access remains an
architectural trust decision. Those are not the only issues left.
