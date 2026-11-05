"""Advisory analysts: free-model subagents that a desk's paid reasoning model can consult.

The orchestration is deliberately narrow:

* **One decision-maker.** The desk agent (DeepSeek) alone reads the account, applies
  its rules and places orders. Nothing here can trade, cancel or journal.
* **Two or three free analysts.** Each is a Nemotron ``:free`` model with a different
  lens (trend, cost/risk, counter-case). They get *no tools*, only a JSON brief of
  precomputed market statistics, so there is nothing for them to reach.
* **Free only, enforced.** The panel refuses to be built with a model that is not an
  OpenRouter ``:free`` model, so a consult can never spend the paid budget.
* **Bounded.** At most three analysts, a per-analyst timeout, output clipped, and one
  consult per desk per ``MIN_INTERVAL_SEC`` (later calls get the last result back), so
  a model looping on the tool cannot burn the free-tier request cap.

Their output is opinion, labelled as such. The statistics are computed here in code
because language models are unreliable at arithmetic; the analysts only interpret them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import statistics
import time
from collections.abc import Awaitable, Callable
from typing import Any

from condor.llm.budget import is_free_model

logger = logging.getLogger(__name__)

#: Tried in order per analyst; a failure falls through to the next. The smaller model
#: leads because the 550B one timed out on every lens in live testing (2026-11-05): its
#: free endpoint is slow, and an analyst brief is worth little if it arrives late.
FREE_ANALYST_MODELS = (
    "openrouter:nvidia/nemotron-3.5-super-vl:free",
    "openrouter:nvidia/nemotron-3-ultra-550b-a55b:free",
)
MAX_ANALYSTS = 3
MAX_SYMBOLS = 3
MAX_QUESTION_CHARS = 300
MAX_BRIEF_CHARS = 1200
#: Per attempt. Worst case is len(models) * this, which must stay under the tool's 150s.
ANALYST_TIMEOUT_SEC = 40.0
#: Measured 2026-11-05 against nemotron-3.5-super-vl:free with the real analyst prompt:
#: with no reasoning setting, or "low", the model spends its whole token budget thinking
#: and returns an EMPTY answer (600/600 reasoning tokens, 0 content characters); with
#: "none" it returns a complete brief in 2-5 s. Advice over precomputed statistics does
#: not need chain-of-thought, and the desk's own (paid) model does the real reasoning.
ANALYST_REASONING_EFFORT = "none"
#: One consult per desk per window. A model that loops on the tool gets the cached answer.
#: Thirty minutes keeps the worst case near 430 free requests a day (both desks flat,
#: consulting every other futures tick and every spot review), well inside the shared
#: 1,000-a-day free-model cap that the desks' own requests also draw on.
MIN_INTERVAL_SEC = 1800.0

COMMON = (
    "You are one of several independent analysts advising a simulated trading desk's "
    "reasoning model. You see ONLY the JSON facts you are given. You cannot trade and "
    "have no tools. Never invent prices, news or events. Every statistic is "
    "precomputed from COMPLETED candles: use it, do not recompute it. If the data does "
    "not support a view, say STANCE: unclear. Reply in under 140 words, exactly in this "
    "form:\n"
    "STANCE: <bullish|bearish|neutral|unclear> - <one line>\n"
    "EVIDENCE: up to three bullets, each citing a number from the facts\n"
    "INVALIDATION: the observable that would prove this view wrong\n"
)

LENSES: dict[str, tuple[str, str]] = {
    "trend": (
        "Trend and structure analyst",
        "Judge each symbol's multi-day trend and structure from the daily and 4-hour "
        "statistics: direction, whether price sits above a rising 20-bar average, "
        "pullback/reclaim or higher-low reversal setups, and the nearest range "
        "high/low as a target or invalidation.",
    ),
    "cost_risk": (
        "Cost and risk auditor",
        "Judge whether any plausible move clears costs. Compare the round-trip cost in "
        "bps (and funding, for futures) with realistic target distances taken from the "
        "volatility and range statistics. State the reward/risk a trade would need.",
    ),
    "counter": (
        "Counter-case analyst",
        "Argue the strongest specific case AGAINST opening a new position now: "
        "choppiness, failed levels, thin volatility, cost drag, an extended move. Be "
        "quantitative and cite the facts. If the case against is weak, say so.",
    ),
}

Runner = Callable[..., Awaitable[str]]


# ── facts (pure, deterministic) ──────────────────────────────────────────


def _f(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _sig(value: float | None, digits: int = 6) -> float | None:
    if value is None or value == 0:
        return value
    return round(value, digits - int(math.floor(math.log10(abs(value)))) - 1)


def _pct(new: float, old: float) -> float | None:
    return None if old == 0 else round((new / old - 1) * 100, 2)


def summarize_candles(rows: list[dict], keep: int = 30) -> dict:
    """Statistics over COMPLETED candles (the last row is still forming and is dropped)."""
    done = [r for r in rows[:-1] if _f(r.get("close")) is not None]
    closes = [_f(r["close"]) for r in done]
    highs = [_f(r.get("high")) for r in done]
    lows = [_f(r.get("low")) for r in done]
    if len(closes) < 5:
        return {"completed_bars": len(closes), "note": "too few completed bars"}
    last = closes[-1]
    window = closes[-20:]
    sma = sum(window) / len(window)
    sma_prior = sum(closes[-25:-5]) / len(closes[-25:-5]) if len(closes) >= 25 else None
    rets = [(b / a - 1) * 100 for a, b in zip(closes, closes[1:]) if a]
    hi = max(h for h in highs[-20:] if h is not None)
    lo = min(v for v in lows[-20:] if v is not None)
    recent_lows = [v for v in lows[-3:] if v is not None]
    earlier_lows = [v for v in lows[-10:-3] if v is not None]
    out = {
        "completed_bars": len(closes),
        "last_close": _sig(last),
        "return_pct": {
            f"{n}_bars": _pct(last, closes[-n - 1])
            for n in (1, 5, 20)
            if len(closes) > n
        },
        "sma20": _sig(sma),
        "close_vs_sma20_pct": _pct(last, sma),
        "sma20_vs_5_bars_ago_pct": _pct(sma, sma_prior) if sma_prior else None,
        "high_20": _sig(hi),
        "low_20": _sig(lo),
        "pct_below_20_high": _pct(last, hi),
        "pct_above_20_low": _pct(last, lo),
        "bar_return_stdev_pct": (
            round(statistics.pstdev(rets[-20:]), 2) if len(rets) >= 2 else None
        ),
        "higher_low_vs_prior_7": (
            min(recent_lows) > min(earlier_lows)
            if recent_lows and earlier_lows
            else None
        ),
        "recent_closes": [_sig(c, 5) for c in closes[-keep:]],
    }
    return out


def build_facts(desk: str, symbol: str, snapshot: dict, daily: dict, h4: dict) -> dict:
    """One symbol's compact brief from a live snapshot plus daily and 4-hour candles."""
    facts: dict[str, Any] = {
        "symbol": symbol,
        "desk": desk,
        "mid": snapshot.get("mid"),
        "spread_bps": snapshot.get("spread_bps"),
        "round_trip_cost_bps": {
            k: v
            for k, v in (snapshot.get("round_trip_cost_bps") or {}).items()
            if k != "note"
        },
        "daily": summarize_candles((daily.get("candles") or {}).get("rows", [])),
        "four_hour": summarize_candles((h4.get("candles") or {}).get("rows", [])),
    }
    if snapshot.get("funding"):
        facts["funding"] = {k: v for k, v in snapshot["funding"].items() if k != "note"}
    return facts


# ── the panel ────────────────────────────────────────────────────────────


def assert_free(models: tuple[str, ...]) -> None:
    for model in models:
        prefix, _, model_id = model.partition(":")
        if prefix != "openrouter" or not is_free_model(model_id):
            raise ValueError(
                f"Analyst models must be OpenRouter ':free' models, got {model!r}: "
                "a consult must never spend the paid budget."
            )


async def default_runner(model: str, system: str, user: str, *, caller: str) -> str:
    """One tool-less, budget-governed completion. Needs no MCP server, so no tools mount."""
    from condor.runtime.llm_client import build_llm_client

    client = build_llm_client(
        model,
        system_prompt=system,
        budget_caller=caller,
        unattended=True,
        reasoning_effort=ANALYST_REASONING_EFFORT,
    )
    await client.start()
    try:
        return str(await client.prompt(user) or "")
    finally:
        await client.stop()


def _clip(text: str) -> str:
    text = str(text).strip()
    # Some free models echo the whole answer a second time. A brief has exactly one
    # STANCE line, so cut at the repeat rather than hand the desk the same text twice.
    repeat = text.find("STANCE:", len("STANCE:"))
    if repeat > 0:
        text = text[:repeat].rstrip()
    return text if len(text) <= MAX_BRIEF_CHARS else text[: MAX_BRIEF_CHARS - 1] + "…"


class AnalystPanel:
    def __init__(
        self,
        runner: Runner | None = None,
        *,
        models: tuple[str, ...] = FREE_ANALYST_MODELS,
        clock: Callable[[], float] = time.time,
        min_interval: float = MIN_INTERVAL_SEC,
        timeout: float = ANALYST_TIMEOUT_SEC,
    ) -> None:
        assert_free(models)
        self._runner = runner or default_runner
        self._models = models
        self._clock = clock
        self._min_interval = min_interval
        self._timeout = timeout
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, tuple[float, dict]] = {}

    async def _ask(self, lens: str, desk: str, user: str) -> dict:
        name, instruction = LENSES[lens]
        system = f"{COMMON}\nYour role: {name}. {instruction}"
        for model in self._models:
            try:
                text = await asyncio.wait_for(
                    self._runner(
                        model, system, user, caller=f"sim_analyst.{desk}.{lens}"
                    ),
                    timeout=self._timeout,
                )
            except Exception as exc:  # one analyst failing must never fail the consult
                logger.warning(
                    "Analyst %s/%s unavailable on %s (%s)",
                    desk,
                    lens,
                    model,
                    type(exc).__name__,
                )
                continue
            if str(text).strip():
                return {
                    "lens": lens,
                    "role": name,
                    "model": model.partition(":")[2],
                    "status": "ok",
                    "brief": _clip(text),
                }
        return {
            "lens": lens,
            "role": name,
            "status": "unavailable",
            "brief": "No free analyst model answered in time. Decide without this lens.",
        }

    async def consult(
        self,
        desk: str,
        facts: list[dict],
        question: str = "",
        lenses: list[str] | tuple[str, ...] | None = None,
    ) -> dict:
        chosen = list(dict.fromkeys(lenses or LENSES))[:MAX_ANALYSTS]
        unknown = [name for name in chosen if name not in LENSES]
        if unknown:
            raise ValueError(
                f"Unknown analyst lens {unknown}; choose from {list(LENSES)}"
            )
        if not facts:
            raise ValueError("A consult needs at least one symbol's facts")
        lock = self._locks.setdefault(desk, asyncio.Lock())
        async with lock:
            now = self._clock()
            held = self._last.get(desk)
            if held and now - held[0] < self._min_interval:
                age = now - held[0]
                return {
                    **held[1],
                    "cached": True,
                    "age_sec": round(age),
                    "next_fresh_consult_in_sec": round(self._min_interval - age),
                }
            user = (
                "Facts (JSON). Completed candles only.\n"
                + json.dumps(facts[:MAX_SYMBOLS], separators=(",", ":"))
                + (
                    f"\nThe desk's question: {question[:MAX_QUESTION_CHARS]}"
                    if question.strip()
                    else ""
                )
            )
            analysts = await asyncio.gather(
                *(self._ask(lens, desk, user) for lens in chosen)
            )
            result = {
                "advisory": True,
                "note": (
                    "Opinions from free models over precomputed statistics. They cannot "
                    "see your account and may be wrong. Verify against your own snapshot; "
                    "never cite an analyst as the only reason for a trade."
                ),
                "desk": desk,
                "symbols": [f.get("symbol") for f in facts[:MAX_SYMBOLS]],
                "as_of": now,
                "cached": False,
                "analysts": list(analysts),
            }
            if any(a["status"] == "ok" for a in analysts):
                self._last[desk] = (now, result)
            return result
