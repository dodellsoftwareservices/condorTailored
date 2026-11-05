# Spend guard and advisory analyst panel — November 5, 2026

Two pieces that work together: a hard monthly dollar ceiling on paid model spend, and a
small orchestration layer in which one paid reasoning model (DeepSeek v4.1 Flash 1017)
consults two or three free Nvidia Nemotron analysts.

## The $30 monthly guard

`agent-config.json` → `budget`:

| Key | Value | Meaning |
| --- | --- | --- |
| `monthlyCostCapUsd` | 30 | UTC calendar-month ceiling on paid spend |
| `requestCostReserveUsd` | 0.20 | Worst case of one paid request, held while it is in flight |
| `paidPriceCeilingUsdPerMillion` | 0.17 in / 0.55 out | OpenRouter `max_price` sent with every paid request |
| `paidMaxContextTokens` | 1,048,576 | Largest prompt the model accepts |
| `allowedPaidModels` | `openrouter:deepseek/deepseek-v4.1-flash-1017` | The only paid model permitted |

How it works (`condor/llm/budget.py`, `condor/llm/cost_model.py`):

- Before a paid request, the governor adds the month's recorded spend, the reservations
  held by requests in flight, and this request's reserve. At or over the cap, the request
  is refused until the first of the next month.
- **Real prices, not a catalogue.** Every OpenRouter request asks for billed usage
  (`usage.include`), and the billed `cost` is what gets recorded. The bundled price
  catalogue does not know `deepseek-v4.1-flash-1017`, so before this change every paid
  response was logged `cost_unknown` and nothing could be capped.
- **The reserve is a proven bound, not a guess.** The paid model is pinned to
  `wafer/mxfp4` with no fallback routing and a price ceiling from the config. The config
  validator computes the worst request that allows (context x input ceiling + 16,384 capped
  output tokens x output ceiling, about $0.187) and refuses to load if
  `requestCostReserveUsd` is lower, or if a monthly cap has no ceiling at all.
- **Keep the ceiling just above the endpoint's real price.** `wafer/mxfp4` was verified at
  $0.16/M input and $0.50/M output on 2026-11-05. A ceiling below the real price does not
  overspend; it makes OpenRouter filter the endpoint out and every request return 404. That
  is exactly what happened on the first restart (the draft ceiling was $0.13), and the
  desks could not think until it was corrected. Check
  `https://openrouter.ai/api/v1/models/deepseek/deepseek-v4.1-flash-1017/endpoints` when a
  paid request starts failing with 404.
- **A provider rejection is not spend.** A 4xx reply (bar 408) means the request was refused
  before the model ran, so it is not booked. Otherwise a mis-set routing option would burn
  the month's cap at the reserve per failed call. Timeouts, 5xx and dropped streams still
  count as uncertain spend.
- **Unpriced responses are booked at the reserve** rather than pausing every paid request
  for the month (a timeout would otherwise have locked paid work until the 1st).
- **Orphaned reservations expire** after 30 minutes, so a crash cannot slowly eat the cap.
- **Free models are exempt** and cannot be routed to a paid endpoint (`max_price: 0`).
- **The baseline is refreshed from the provider, automatically.** `data/llm_budget/spend-baseline.json`
  books spend the local ledger did not see: spend from before metering, and spend by anything
  else holding the same key. `condor/llm/provider_usage.py` reads OpenRouter's
  `GET /api/v1/key` (`usage_monthly`, `usage_daily`) every ten minutes and rewrites the
  baseline from it. It never loosens: a failed read keeps the baseline, a figure below the one
  already held for the month (reporting lag) is ignored, and the new baseline overlaps the
  reading by three minutes so a request that just finished is counted twice rather than not at
  all. The file is local, not committed.
- **Watching it.** `GET /api/v1/llm/spend` (admin) returns the cap, what counts, what is held
  in flight and the headroom; the dashboard shows it as one line that turns to the attention
  colour under a fifth of the cap and says so plainly when paid requests are paused.
- **The request caps are sized for the load.** `dailyRequestCap` is 2,500, not the 1,000 a
  single free-model desk needed: the two desks plus the analysts come to roughly 1,200-1,700
  requests a day, and `appliesTo: all` counts paid requests too. Paid ones are bounded in
  dollars; free ones cannot reach OpenRouter's own 1,000-a-day free limit on their own.
- OpenRouter can also enforce its own limit on the key (`limit`, `limit_remaining` in the
  same `/key` reading), which sits outside the $30 guard as a provider-side backstop.
- A subscription (ACP) client cannot be metered, so it is refused while a dollar cap is on.

Measured on 2026-11-05: about $0.001 per DeepSeek request, $4.96 spent in the month before
the guard went live. At the current request rate the cap is reached near month-end.

## The analyst panel

```
desk agent (DeepSeek, paid)  ── sim_consult_analysts ──►  Condor
   the only decision-maker                                 fetches daily + 4h candles
   places every order                                      computes the statistics in code
                                                           ├─ trend analyst        (Nemotron, free)
                                                           ├─ cost/risk auditor    (Nemotron, free)
                                                           └─ counter-case analyst (Nemotron, free)
```

- **No tools, no account access.** Analysts receive a JSON brief of precomputed statistics
  (trend, volatility, range, round-trip cost, funding) and return a short opinion. They
  cannot trade, cancel, journal or call anything, and never see balances or positions.
- **Why a dedicated tool and not `delegate`.** The simulator seats are sealed: a test forbids
  them from holding `delegate`, because delegated agents run with auto-approved tools. This
  tool gives the desk advice without handing it a way to reach anything real.
- **Free only, enforced.** The panel refuses to be built with a model that is not an
  OpenRouter `:free` model, so a consult cannot spend the paid budget.
- **Bounded.** At most three analysts and three symbols, 40 seconds per attempt (a healthy
  call takes about 3 seconds), each brief reduced to plain text (no links or control
  characters, 14 short lines) because it is untrusted model output on its way into the paid
  desk's context. The same symbols and lenses are served from cache for 30 minutes, and a
  desk may start at most two fresh consults per half hour, answered or not, so a model
  looping on a failing tool cannot burn the free tier (`throttled: true` after that).
- **Degrades quietly.** If an analyst model is slow or down it falls through to the second
  free model, then reports `unavailable` and the desk decides without that lens.
- **Advisory.** The tool result and both desk playbooks say so; a brief may never be the
  only reason for a trade, and the journal records which brief changed the decision.

Live check, 2026-11-05, and what it taught:

- First attempt: the analysts timed out or came back empty (0 of 3 in 81 s). The cause was
  not the governor or the network. Probed directly against `nemotron-3.5-super-vl:free`
  with the real prompt: with no reasoning setting, or `low`, the model spends its whole
  token budget thinking and returns an **empty answer** (600/600 reasoning tokens, 0 content
  characters). With `reasoning.effort: none` it returns a complete brief in 2-5 s.
- The analysts therefore run with `effort: none` (`ANALYST_REASONING_EFFORT`), set per
  client so the desks keep the global `high`. Result: 3 of 3 analysts over three symbols in
  about 11 s, with briefs that cite the supplied statistics.
- Timeouts of free-model calls were also being booked as unpriced spend at the $0.20
  reserve; a free model never books spend now, whatever happens to the request.
- Some free models echo the whole answer twice; the panel cuts at the repeated `STANCE:`.

## What this does not establish

The analysts add a second and third opinion; they do not add an edge. Neither desk has shown
one yet, and a few days of simulated trading proves nothing about future profit.

## Operating notes

- **Restarting.** Code changes need a restart (only `handlers/` and `routines/` hot-reload).
  Use `make restart` (it stops only the `condor` tmux session). Both desks and the market maker then resume by
  themselves. Boot reconciliation used to restart loops before the web server existed, so every
  resumed loop failed its first tick (the simulator tools are HTTP clients of that server); it
  now waits for the dashboard port. Do not also start the loops by hand: the manual start adds
  a second instance rather than replacing the first.
- **A paid request failing with 404** almost always means the price ceiling is below the
  endpoint's current price. Check the endpoints URL above and raise
  `paidPriceCeilingUsdPerMillion` (and `requestCostReserveUsd` with it; the config validator
  will tell you if the reserve no longer covers the worst case).
- **Subscription (ACP) agents are refused while the cap is on**, because their spend cannot be
  metered. The agents that use `claude-acp:*` / `claude-code:*` keys will not start until the
  monthly cap is removed.
