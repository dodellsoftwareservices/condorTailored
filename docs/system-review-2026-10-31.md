# System review — usability, security, connectivity, cost (2026-10-31)

> Historical snapshot. November 1 remediation and its remaining gaps are audited
> in [the November 2 verification](system-review-verification-2026-11-02.md).
> In particular, the live P0 status and “not fixed” labels below are not current.

Two independent reviews of the same codebase, synthesized into one. The first
(Claude/Sonnet 5.5) ran four parallel code-reading audits, one per axis, each
verifying claims against the actual source rather than file names. The second
(GLM 5.3) reviewed the same request but checked the *live running system* —
process logs, actual `config.yml` values, `git status` — not just the code at
rest. That difference in method is why it caught a live outage the first
review's static reading missed entirely, and why this document treats both
as first-class rather than picking one.

Every claim from both source reviews that could be spot-checked against this
machine's actual files/logs was re-verified while writing this synthesis
(exact commands and results below, where it mattered). Nothing here is
restated from either review without that check, except where explicitly
marked "not independently re-verified."

---

## P0 — found live, fixed during this review

**Condor could not authenticate to the hummingbot-api server, and had been
failing on every tick since this session's own password rotation.**

The live tmux log showed this repeating every ~30 seconds:

```
2026-10-31 19:53:40 - config_manager - ERROR - Failed to connect to 'local':
  401, message='Incorrect username or password', url='http://localhost:8000/accounts/'
```

Root cause, confirmed directly: earlier in this session, `hummingbot-api`'s
own HTTP Basic Auth `PASSWORD` was rotated from a weak default to a strong
32-character value, as part of fixing the hardcoded-password findings below.
That rotation updated `hummingbot-api/.env` but never propagated to
`condor/config.yml`'s `servers.local.password` — the credential Condor itself
uses *as a client* to call that same server. Verified by length comparison
(never printing either secret):

- `hummingbot-api/.env` → `PASSWORD` = 32 characters (the rotated value)
- `condor/config.yml` → `servers.local.password` = 6 characters (stale)

**Every routine that touches the Hummingbot API — `price_monitor`,
`triangular_arb`, `arb_check`, market data for the MM/grid agent loops — has
been failing on every single tick since that rotation**, each failure
landing in `data/notifications.json` as an unremarkable ❌ entry rather than
anything that would get an operator's attention. This is the review's
single most important finding across all four axes: the system's failure
mode for "the trading backend is completely unreachable" is a slow drip of
red dots in a notification bell, not an alarm.

**Fixed during this review**: `condor/config.yml`'s stored password for the
`local` server was synced to the real 32-character value (via a script that
never printed either secret, with a pre-edit backup kept at
`config.yml.bak-pre-password-sync` in this session's scratchpad — not in the
repo). This fix is **on disk but not yet live** — the running Condor process
loaded the old 6-character password into memory at boot and reads
`self._data["servers"][name]["password"]` from that in-memory copy, not from
disk, on every connection attempt. **Condor needs a restart to pick up the
corrected credential.** That restart was intentionally not done as part of
this review, since it drops the live dashboard/bot session — it needs the
operator's go-ahead.

---

## Security

### Critical

**1. `POST /auth/2fa/enroll` can be used to silently disable and re-arm a
victim's 2FA without proving possession of it — a bypass of the entire
feature, introduced this session.**
`condor/config_manager.py:1154-1176` (`enroll_totp`) unconditionally
overwrites the stored secret and clears the `enabled` flag, with **no**
check that the caller can prove the *current* code — unlike `disable_totp`,
which correctly requires one (`condor/condor/web/routes/auth.py:245-266`).
Anyone riding a victim's live session (stolen cookie, XSS, an unattended
device) can call `enroll`, receive a brand-new secret in the response, derive
a valid code from it, and call `confirm` — re-enabling 2FA under an
attacker-controlled secret while the real owner's authenticator now does
nothing. This defeats 2FA's actual purpose (protection that survives
compromise of the session itself) using only the session.
**Not yet fixed.** The fix is small: `enroll_totp` should require the
current TOTP/recovery code before re-enrolling, exactly like `disable_totp`
already does, whenever 2FA is already enabled on that account.

### High

**2. Zero rate limiting or lockout on any auth endpoint.**
`/auth/token-login`, `/auth/local-login`, `/auth/2fa/confirm`,
`/auth/2fa/disable` — confirmed no inbound-auth limiter exists anywhere in
the codebase (the only rate limiters that exist gate *outbound* calls to
exchanges/LLMs/data providers). A 6-digit TOTP code is roughly 1-in-333,000
per attempt at `valid_window=1`; unthrottled, a sustained attack from one IP
can realistically land a hit within hours, with no lockout, delay, or alert
ever firing.

**3. The guided "remote VPS, no Tailscale" setup path produces a plaintext,
publicly-bound dashboard with no warning.**
Neither Condor's web server nor hummingbot-api's FastAPI server configures
TLS anywhere. `utils/config.py:151` (`resolve_web_host`) defaults to binding
`0.0.0.0` for Telegram-mode-without-Tailscale; `setup-environment.sh:847`
defaults the Tailscale prompt to **No**, then immediately writes
`WEB_URL=http://<ip>:8088` with no warning — contrast with local mode's
explicit `local_mode_warning()` a few lines away. In that (apparently
common) configuration, the login token, session cookie, and
hummingbot-api's Basic Auth header all travel in cleartext over the open
internet.

**4. `/var/run/docker.sock` is mounted read-write into the hummingbot-api
container, which also runs as root — and this is load-bearing, not
leftover.**
Confirmed genuine use: `services/bots_orchestrator.py:30`,
`services/docker_service.py:35`, `services/gateway_service.py:39`,
`services/self_upgrade.py:439` all use it for bot orchestration and
self-upgrade. Docker socket access is unconditionally host-root-equivalent
regardless of in-container UID. Compounding factor: `self_upgrade.py:406-413`
pulls and runs new `hummingbot/hummingbot-api:latest` image tags with no
signature/digest verification beyond its own staleness check. Any RCE in
that process, or any upstream image compromise, is immediate full host
takeover — not something worth "fixing" outright (the orchestration feature
genuinely needs it), but worth the owner explicitly accepting this tradeoff
rather than it being an implicit fact nobody wrote down until now.

### Medium

**5. A non-admin, approved user can make the server probe arbitrary
internal/loopback hosts — an authenticated SSRF/internal-scanner primitive.**
`condor/web/routes/settings.py:1068` (`add_custom_provider`) is gated only by
`get_current_user`, not `require_admin` — inconsistent with this codebase's
otherwise-careful pattern of admin-gating anything that reaches outside the
app. It drives a real outbound GET to a caller-supplied `base_url`; loopback
and RFC1918 ranges are allowed by default (`CUSTOM_LLM_BLOCK_PRIVATE_URLS` is
opt-in), handing any approved seat a working scanner against the host's
loopback and Docker network — including hummingbot-api's own Basic-Auth
port, Postgres, and EMQX. The guard's own comment admits it's "defeatable by
DNS rebinding... not a security boundary."

**6. CSRF protection is SameSite=Lax alone, with no defense in depth, and
the comment justifying it argues backwards.**
`condor/condor/web/auth.py` sets the session cookie `SameSite=Lax` with no
CSRF token, no Origin/Referer check, and no custom-header requirement on
state-changing requests. SameSite=Lax is a real, adequate control for this
app's actual request shape (no legitimate cross-site POST flow exists that
needs the cookie attached) — so the practical risk here is low. But the
comment in the code argues *because* there's no cross-site POST flow that
needs the cookie, Lax is sufficient — which has the logic backwards: SameSite
exists specifically to stop an *attacker's* cross-site flow, not to describe
the app's own legitimate ones. Worth rewording for the next reader even
though the underlying posture is fine; a second layer (Origin check on
mutating routes) would be cheap insurance regardless.

**7. JWTs are irrevocable for their full 24-hour life.**
`create_jwt` carries no `jti` and there is no server-side session/revocation
table. `logout` only clears the cookie client-side — the token itself
remains valid at the server until natural expiry. A stolen token's blast
radius is a full day, not "until the owner notices and logs out."

### Low

- `routes/reports.py:120` and `frontend/src/lib/api.ts:4462` both still
  describe fetching report HTML "with an Authorization header" — stale
  documentation of the auth transport this session's cookie migration
  replaced; functionally harmless (`get_current_user` still accepts either),
  just inaccurate for the next reader.
- The WebSocket `?token=` query-param fallback
  (`condor/condor/web/auth.py:143`, `routes/chat_ws.py:421-425`) is marked
  deprecated in its own comment but still accepted, and query params land in
  proxy/access logs. The subprotocol and cookie paths both work; this is
  pure legacy surface that could be removed.
- `run_code` executes arbitrary Python with the unrestricted API client
  available to it. The risk engine's own comments (SEC-616, SEC-626) concede
  this is "the way to mutate the world for real," mitigated only by a
  refusal allowlist inside constrained sessions. With `approvalPolicy:
  "never"` configured, the model's discretion there is bounded only by that
  list — documented and tested, but a wide door with a sophisticated latch,
  not a wall.
- hummingbot-api serves Swagger (`/docs`) unauthenticated. Confirmed
  loopback-only bound today, so informational — but if `WEB_URL`/Tailscale
  ever widens that bind, the full API surface becomes self-documenting to
  anyone who can reach it.
- `frontend/package.json` pins `lucide-react` to `^1.0.1` while every sibling
  dependency tracks current-generation releases — worth a one-time check
  it isn't an orphaned pin (not independently re-verified against upstream).

### Confirmed clean (stated plainly, not padded with non-findings)

No hardcoded secrets anywhere in current code or git history across
condor/, hummingbot-api/, or mcp_servers/ (`config.yml`/`config.yml.bak`/
`.env` are correctly gitignored — confirmed in `.gitignore` directly — and a
full-history grep found nothing, including pre-rotation). No `shell=True`,
`os.system`, or real `eval`/`exec` of external input in application code.
Exchange credentials never echo in plaintext to a log, response, or error
path. MCP servers always re-derive role/permission from `ConfigManager` at
call time rather than trusting a JWT claim, closing the obvious
self-escalation path. CORS is an explicit origin allow-list, never a
wildcard. `python-jose`'s decode call pins `algorithms=["HS256"]` explicitly,
closing the classic JWT algorithm-confusion bug class regardless of the
library's own history. Alpaca integration is hard-restricted to paper
trading at construction time — no live-trading code path exists. The
already-fixed items from earlier in this session (Postgres/EMQX/
CONFIG_PASSWORD rotation, gateway cert file permissions, the `shell=True`
removal in `setup_llm.py`, the cookie-based browser auth migration) were all
spot-checked here and found correctly and completely applied, with the one
glaring exception of the config.yml sync gap documented as the P0 above.

---

## Connectivity & reliability

### High

**8. `depends_on` doesn't gate on health for either of hummingbot-api's real
startup dependencies.**
`hummingbot-api/docker-compose.yml:53-54` uses plain-list `depends_on:
[postgres]`, which only waits for the container to *start*, not for its
`pg_isready` healthcheck to pass — and hummingbot-api isn't declared
dependent on `emqx` at all despite needing `BROKER_HOST=emqx` immediately.
Self-heals via `restart: unless-stopped` into a flaky first boot rather than
a hard outage, but it's a one-line fix (`condition: service_healthy` on
both).

**9. The default non-Tailscale remote deployment binds the API to
`0.0.0.0`.** Same underlying fact as security finding #3, repeated here
because it's as much a network-topology default as a security one.

### Medium

**10. `mean_reversion_scanner.py` hardcodes `api.binance.com`/
`fapi.binance.com`, and `price_monitor.py` defaults to the same venue —
both a US-regulatory landmine, not a transient outage.**
Confirmed directly: `routines/mean_reversion_scanner.py:14-16` hardcodes
`BINANCE_SPOT_TICKER`/`BINANCE_SPOT_KLINES`/`BINANCE_FUTURES_INFO` against
`api.binance.com`/`fapi.binance.com`; `routines/price_monitor.py:25` defaults
`connector: str = Field(default="binance", ...)`. A US-IP request to either
Binance global endpoint gets HTTP 451 — permanently, not intermittently —
and the live `data/notifications.json` already has evidence of exactly this:
`✅ Routine mean_reversion_scanner done — Failed to fetch tickers: 451`. That
checkmark is the real bug here, not just the hardcoded host: the routine
caught the failure, returned a string describing it, and the scheduler
reported success. **Status semantics are string-return-based, not
exception-based** — "done" means "didn't crash," not "produced a signal."
Any routine that swallows an error into its return string reports green.

**11. No timeout wraps yfinance calls, and they run on Python's shared
default thread pool.** `condor/equity_market_data.py` — `yf.Search`,
`Ticker.info`, `.history()` all run via bare `asyncio.to_thread` with no
wrapping timeout, and yfinance's own `requests` session sets none either. A
hung Yahoo request occupies one thread-pool slot indefinitely, shared by
every other `to_thread` call in the process. The existing stale-cache +
`SingleFlight` coalescing reduces how often this path is hit but doesn't
bound a single in-flight call.

**12. Most `aiohttp` sessions in `condor/fetchers/` are created without an
explicit `ClientTimeout`**, which means aiohttp's 5-minute default applies
per request, on the same event loop as everything else — a single hung
connection can stall a tick for up to 300 seconds. The explicit-timeout
discipline that exists elsewhere in this codebase (GeckoTerminal's full
429-aware breaker in `condor/pool_data.py:394-524`, Alpaca's 20s
`ClientTimeout` in `alpaca_paper.py:46`) is real and well-built — it's just
not the norm across every fetcher.

**13. `GATEWAY_URL=https://gateway:15888` is a dangling hostname until
Gateway is explicitly started**, and the error it produces in that state
(an SSL/PEM error, previously investigated and judged benign) is misleading
rather than a clear "Gateway is not running." Expected behavior given
Gateway's dynamic-start design (`services/gateway_service.py:204-324`), not
a bug — but worth a clearer error message at the call site.

**14. No aggregate "this venue is down" alarm or failure deduplication.**
The P0 above produced the same `ConnectionError` text, logged and
notification-posted, roughly once every 30 seconds for an unknown but
clearly extended period — there is no circuit breaker at the
notification/alerting layer that collapses "the same failure, N times in a
row" into one actionable alert the way `condor/pool_data.py`'s GeckoTerminal
breaker does for its own domain.

**15. Everything runs on one process, one event loop** (dashboard,
scheduler, agent engine, fetchers, persistence, in `main.py`), and
`SafePicklePersistence`/`ConfigManager` do synchronous file I/O
(`yaml.dump`, pickle writes) inside that same loop. Fine at current scale — a
blocking write is a tens-of-milliseconds stall today — but it's a design
ceiling, not a bug, worth knowing about before scaling up usage.

### Low

- `_run_dual()` in `main.py` doesn't specifically supervise the Telegram
  polling task — if it died outside PTB's own internal retry handling, the
  web dashboard would keep reporting healthy while the bot went silently
  deaf on Telegram. Low likelihood (PTB is generally resilient internally),
  no explicit supervision exists for this exact failure mode either way.
- `routines/error_test.py` ("Routine that intentionally fails") ships live
  in the routine library, and `scripts/register_testnet_credentials.py` is a
  testnet-era leftover — both harmless clutter, not functional bugs.

### Confirmed well-handled

WebSocket reconnection (`frontend/src/lib/websocket.ts:80-123`) uses real
exponential backoff (1s→30s cap), resets only on an actual successful
`onopen`, and runs through a single shared socket per tab with correct
resubscribe — no reconnect-storm risk. All three WS auth paths (subprotocol,
query param, the new cookie fallback) fail closed with zero path to an
unauthenticated connection. Condor's own client to hummingbot-api has real
fail-fast failure caching and consistent error mapping across routes.
GeckoTerminal/Alpaca calls have real timeouts and correctly skip retrying a
429. MCP servers are stdio-only with no network attack surface. The APScheduler
boot-reconciliation path (`main.py:589-626,752-806`) correctly restores
scheduled jobs, reports interrupted runs, and notifies the owner once — a
detail most hobby schedulers get wrong.

---

## Usability

### Critical

**16. 2FA has no recovery path and no admin escape hatch.** There is no
admin route anywhere that calls `disable_totp` on another user's behalf —
only self-service, which requires the exact code or recovery list the user
has lost. A user who loses their authenticator *and* their recovery codes is
locked out of their own dashboard, permanently, with no documented recovery
beyond hand-editing `config.yml` on the server — which is written down
nowhere.

**17. Nothing forces or confirms the recovery codes were actually saved.**
`frontend/src/components/settings/SecuritySettings.tsx:188-211` — the
Confirm form is live and submittable the instant the enrollment screen
renders, with no "I've saved these" acknowledgment step. Combined with #16,
this turns a theoretical lockout into an easy accidental one.

### High

**18. The onboarding doc describes a different product than what's
running.** `condor/docs/get-started.md` says "Condor" throughout and never
once says "Griffin" — the live app (mid-rebrand, done by other work in this
same tree) now renders the name "Griffin" with a different logo. A
brand-new operator, this doc's exact audience, opens the app to find it
doesn't match the guide in front of them.

**19. The new 2FA feature is entirely undocumented**, and the onboarding
doc's own "Good habits" section still flatly states "the portal has no
password" — true only by default — with zero mention that a Security tab
exists, what turning it on does, or the lockout risk in #16.

**20. `make restart` (and `run`/`stop`) manage only Condor's own tmux
process — never the Docker stack (`hummingbot-api`, `hummingbot-broker`,
`hummingbot-postgres`) that the P0 outage above lives in.** Confirmed
directly in the Makefile: `restart` is `stop` + `run`, and both only touch
the `condor` tmux session. The operator has to already know there are two
independent runtimes for this to make sense. There *is* a real diagnostic
for the missing half — `make doctor` (`condor/doctor.py:787`,
`check_hummingbot_api()`) specifically checks whether the hummingbot-api
server is reachable and authenticating, and even has a dedicated check for
exactly the placeholder/stale-credential class of bug the P0 above is
(`doctor.py:269-286`). So the real gap isn't "no health tool exists" — it's
that **nothing runs it automatically or surfaces its result as an alert**
between manual invocations; a correct diagnostic that nobody is prompted to
run doesn't help during the 20+ minutes (observed live) this exact failure
mode went unnoticed.

### Medium

- `hummingbot-api/` ships no `.env.example` at all (only `condor/.env.example`
  exists) — a fresh clone hits a bare Docker Compose error
  (`${VAR:?Set POSTGRES_PASSWORD in .env}`) with no template to copy from, a
  direct side effect of this session's own hardening of those variables.
- `SecuritySettings.tsx`'s clipboard-copy button silently no-ops on any
  non-HTTPS, non-`localhost` deployment (`navigator.clipboard` is undefined
  in that case) — exactly the Tailscale/plain-HTTP topology this codebase
  explicitly supports. A user may believe they copied their recovery codes
  when nothing happened.
- Loading-state UI is inconsistent: `Bots.tsx` uses the shared
  `FallbackSpinner`; `Portfolio.tsx`/`Equities.tsx` each roll their own
  inline markup. Several error paths in `agents.py` (lines 2879, 2943,
  2966, 3148, 3696) pass `detail=str(exc)` straight to the client — raw
  Python exception text — inconsistent with the better-written,
  plain-language messages elsewhere in the same file.
- `maxSteps: 1000` with `tickTimeoutSec: 900` in `agent-config.json` gives a
  runaway agent up to 1000 steps or 15 minutes before either limit bites —
  a lot of rope; the per-run request cap (100) is the leash that actually
  matters in practice.
- **253 files currently uncommitted** (confirmed via `git status --short`),
  including today's Hyperliquid removal, the 2FA work, and fixture
  regeneration from other concurrent work in this tree. `git log` HEAD is
  still at an old commit. Until this is committed, there is no rollback
  point — a careless `git checkout .` would resurrect code the test suite
  now assumes is gone.

### Low

- The Login page's OTP input has no accessible label (placeholder-only).
- `CONDOR_MODE` isn't documented in `condor/.env.example` — only
  discoverable via the setup wizard or a runtime error pointing at it.
- The top-level README reads in Hummingbot's own upstream voice (survey
  links, general marketing) rather than describing this specific
  deployment — harmless, but it's describing a product category, not this
  install. `CLAUDE.md`/`docs/` were accurate against the code everywhere
  they were spot-checked.

### Confirmed well-handled

`make setup`'s interactive flow is genuinely good — no hardcoded path
assumptions, idempotent `.env` writes, a short path to a first working
signal with the free-tier default model. The failure-notification pipeline
itself (`data/notifications.json`, linked reports) is a reasonable pattern
when the underlying failure is something other than "the entire backend is
unreachable" — the gap is specifically in escalation for a repeating,
systemic failure, not in the notification mechanism's basic design.

---

## Cost

Cash cost today is effectively $0/month; the real exposure is operational
risk if the configuration changes, not a bill.

### High

**21. No dollar-based LLM spend cap, only a request-count cap.**
`condor/llm/budget.py`'s `Budget` dataclass has no cost field at all;
`condor/acp/usage.py` computes `cost_usd` per run, but nothing anywhere
reads it as a ceiling (confirmed by grep — zero consumers of that field as a
limit). The governor itself is real and well-built: file-backed, per-caller
accounting, 429-aware cooldown, `dailyRequestCap: 1000` /
`perRunRequestCap: 100`, and a repo-wide grep found no LLM call path that
bypasses it. But the cap is on *requests*, not *dollars* — the configured
model today is a free tier (`openrouter:nvidia/nemotron-...:free`, confirmed
via the live usage ledger: 12 requests in 3 days, $0.00), so this is
currently moot. It stops being moot the moment the model is pointed at a
paid, large-context provider: `maxOutputTokens: null` and
`thinking.effort: high` are both unbounded-by-default settings that would
make a single paid call expensive before the request cap ever notices.

**22. Conversation history grows without trimming — O(N²) resend cost on
a long-running paid model.**
`condor/acp/pydantic_ai_client.py:534` — `self._message_history` is extended
every turn and never pruned, reset only by recreating the client entirely.
With `maxSteps: 1000`, a late tick in a long run resends hundreds of prior
ticks' worth of accumulated history on every single subsequent request —
harmless in token terms on a free model, a real multiplier on a paid one.

**23. Conversation storage on disk is never pruned, by the code's own
admission.** `condor/runtime/conversations.py:525-527` states directly that
the store is never pruned, so it grows for the life of the install; the
overflow archive (`transcript_archive.jsonl`) is append-only with no
rotation either. A long-lived, heavily-used install accumulates disk
indefinitely with no cleanup job or operator warning. (Other stores are
better-behaved: run history prunes past `DEFAULT_RETENTION_DAYS`
(`run_history_store.py:31-57`), routine instances prune
(`routine_store.py:499-527`), and the audit log is still correctly capped at
500 entries, trimmed in place — confirmed still true.)

### Medium

- No CPU/memory limits anywhere in `hummingbot-api/docker-compose.yml` for
  `postgres`, `emqx`, or `hummingbot-api` — a runaway process has no ceiling,
  which is a real cost line on a sized cloud VM even though it's free on a
  home machine.
- Log rotation (`logging: driver: json-file, max-size/max-file`) is set on
  only 1 of the 3 containers (`hummingbot-api`, capped at 30MB); `emqx` and
  `postgres` use Docker's uncapped default.
- No duplicate-instance guard on continuous routines — starting the same
  monitor twice (the codebase documents multi-instance support as a
  feature) doubles call volume against both the backend and, transitively,
  any exchange/data API it polls, with nothing to stop it.
- `alpaca_paper.py`'s `latest_quote()` has no caching at all, unlike its
  yfinance siblings, and is polled every 10s per open symbol tab — trivial
  at current single-operator scale, but the one external-data path in the
  equities feature with no TTL cache.

### Confirmed well-bounded

Alpaca is hard-restricted to paper trading at construction time — no
live-trading cost path exists in code. yfinance paths are genuinely
well-cached (30s/300s/3600s TTLs, size-capped, stale-serve fallback on rate
limit). Infra is 3 local Docker containers on loopback-only ports plus
GeckoTerminal's free tier — electricity is the only real infrastructure
cost today. The LLM budget governor, despite the dollar-cap gap above, is a
genuine single chokepoint with no bypass found anywhere in the codebase.

**The line item that actually matters**: `approvalPolicy: "never"`
(simulated-autonomy mode) combined with the MM loop's own risk bounds
(`max_position_size_quote: 500`, `max_global_drawdown_quote: 100`) describes
a deliberately bounded, unattended-trading posture — which is currently moot
only because the P0 outage above means no order can be placed at all right
now. The day that credential is fixed and live, `approvalPolicy: "never"`
means the first live order executes with no human in the loop by design.
That's a decision worth the operator explicitly re-confirming now that the
P0 fix is about to make it live again, not a bug to fix.

---

## Consolidated, ranked

| # | Finding | Severity | Status |
|---|---|---|---|
| 1 | Condor's stored hummingbot-api password is stale after this session's own rotation — every credentialed routine failing on every tick | **P0, live** | **Fixed on disk; needs a Condor restart to take effect** |
| 2 | `/auth/2fa/enroll` can silently disable + re-arm a victim's 2FA with no proof of the current code | **Critical** | Not fixed |
| 3 | No recovery path or admin escape hatch for a user who loses their 2FA device *and* recovery codes | **Critical** | Not fixed |
| 4 | No rate limiting/lockout on any login or 2FA endpoint | High | Not fixed |
| 5 | Default remote-VPS setup path is plaintext HTTP on `0.0.0.0`, no warning | High | Not fixed |
| 6 | `/var/run/docker.sock` + root container = architecturally-necessary host-root exposure | High | Accepted risk — owner should confirm knowingly |
| 7 | Hardcoded Binance global endpoints (451 for US IPs) + `binance` default connector | High | Not fixed |
| 8 | Routine status is string-return-based — a caught error can report ✅ | Medium | Not fixed |
| 9 | No $-based LLM spend cap, only a request-count cap | High | Not fixed (moot while on the free model) |
| 10 | Conversation history/disk grow without trimming | High | Not fixed |
| 11 | `depends_on` doesn't wait for Postgres/EMQX health | High (reliability) | Not fixed |
| 12 | Onboarding doc and 2FA feature are undocumented/out of date vs. the running app | High (usability) | Not fixed |
| 13 | 253 uncommitted files, no current rollback point | Medium | Not fixed (not this review's call to make) |
| 14 | Non-admin SSRF/internal-scanner primitive via custom LLM provider config | Medium | Not fixed |
| 15 | CSRF rests on SameSite=Lax alone; JWTs irrevocable for 24h | Medium | Not fixed |
| 16 | No aggregate "venue is down" alarm/dedup; `make doctor` would catch it but isn't run automatically | Medium | Not fixed |
| 17 | No per-container resource/log limits; most fetchers have no explicit timeout | Low–Medium | Not fixed |
| 18 | Deprecated WS `?token=` fallback, stale comments, `error_test.py`/testnet script left in the library, `lucide-react` version anomaly | Low | Not fixed |

---

## What this says about the codebase overall

The engineering quality underneath all of this is genuinely high — the risk
engine, the budget governor, the scheduler's boot-reconciliation, and most
of this session's own security hardening are better-reasoned (with SEC/FEAT
IDs in the comments explaining *why*) than a lot of commercial code gets.
The failures found here are concentrated at the edges: operational
visibility (a real diagnostic exists for the P0 but nothing runs it
automatically), a few defaults that assume a trust the deployment doesn't
actually have (TLS, rate limiting, the one 2FA endpoint that forgot its own
pattern), and documentation that hasn't kept pace with a codebase under
active, concurrent development. None of the above is architecturally hard
to fix — most of it is a few hours of work per item.

---

## Appendix: using Google Authenticator with the new 2FA (operator guide)

Google Authenticator is a free phone app that generates a new 6-digit code
every 30 seconds. That code is typed in alongside a login so that even a
stolen password alone isn't enough to get in.

**1. Install it.** Get "Google Authenticator" from the App Store (iPhone) or
Play Store (Android) and open it once.

**2. Turn on 2FA in the dashboard.** Go to **Settings → Security** and click
**Enable 2FA**. It shows:
- A long text code (the "secret")
- A link starting with `otpauth://...`
- 8 recovery codes

**3. Add it to Google Authenticator.** In the app, tap **+** →
**"Enter a setup key"** — not "Scan a QR code"; this feature doesn't
generate a scannable QR image yet, only the text form. Then:
- Account name: anything recognizable, e.g. "Griffin"
- Key: paste in the secret from step 2
- Type: "Time based"

Tap Add — the app immediately starts showing a 6-digit code that changes
every 30 seconds.

**4. Save the recovery codes before going any further.** Copy the 8
recovery codes somewhere durable (a password manager, a written note).
This is not optional busywork: per finding #3 above, **there is currently no
admin override or recovery path** — losing the phone and the codes together
means permanent lockout until someone hand-edits `config.yml` on the
server. Treat them like a spare house key until that gap is fixed.

**5. Confirm.** Type the current 6-digit code from the app into the
"Confirm" box in the dashboard and submit. 2FA is now on.

**From then on**: logging in asks for a code once, and — per how it was
built — won't ask again until the next day. Open the app, read the current
code, type it in.

Given the lockout risk in step 4, consider testing this on a throwaway/admin
account first, or triple-check the recovery codes are saved before relying
on it day to day.
