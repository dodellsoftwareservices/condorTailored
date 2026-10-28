---
name: hft_volume_risk
description: "Formula-driven risk assessment for high-frequency market making and arbitrage: volume-clock estimators, fill-intensity calibration, VPIN toxicity, cost-aware Avellaneda-Stoikov/GLFT quoting, liquidity-adjusted tail risk, kill-switch calibration, and a multiplicative GO/THROTTLE/HALT gate."
when_to_use: "Before deploying, resizing, or tuning any pmm_mister / market-making / triangular-arbitrage book, and every tick of a loop that runs one. Also when asked: is this pair safe to quote, how wide should spreads be, how much inventory is too much, what should the drawdown kill switch be, is this arbitrage real after fees and latency, or how large a position the volume can absorb."
created: '2026-10-27T19:00:00Z'
source: chat
---

# HFT Volume Risk Assessment

**Seat.** `market_making_expert` owns the verdict and the deploy/halt decision. Any other
agent that reads this must hand the decision to it.

**What you produce:** one risk verdict per book — `state` (GO / THROTTLE / HALT), a size
`multiplier` in [0, 1], the `binding_gate`, and the derived quoting/limit numbers (schema in §8).

**Scope, honestly.** Condor loops tick every 60-3600 s and Hummingbot executors act at ~1 s
through exchange REST/WS. That is second-scale market making, not sub-millisecond HFT: you
cannot win queue position, so fill intensity must be *measured from your own fills*, never
assumed from the tape. The formulas below are valid at that scale.

The kernel that computes everything is in `risk_kernel.md` (tested; see `derivations.md`
for sources, derivations and the validation record).

---

## 0. Ground rules

1. **Compute with `run_code`, never by hand.** Read the kernel once:
   `manage_skill(action="read_file", name="hft_volume_risk", file="risk_kernel.md")`, paste only
   the functions a step needs. In a dry run `run_code` does not execute: say which numbers you
   would compute and stop at HALT-until-measured.
2. **Units are the whole game.** Money in quote currency, size in base units, time in seconds.
   `sigma_ret` is per-√second and dimensionless; `sigma_abs = sigma_ret × mid` is price per √second.
   Mixing them is the commonest silent error. Print every number with its unit.
3. **Every input is measured, estimated, or assumed — label it.** An assumed input never
   passes a gate that matters. Report `n` and the standard error for every fit.
4. **Lower confidence bounds, not means,** for edge and fill rate. A gate that passes on a
   point estimate with n < 300 is a coin flip.
5. **Re-assess on cadence:** gates every tick; σ every tick (EWMA); VPIN percentile every bar;
   κ and A after every ≥ 500 own fills or daily; Kupiec backtest weekly.

## 1. Inputs and where they come from

| Input | Source | Tier |
|---|---|---|
| OHLCV + `quote_asset_volume`, `n_trades`, `taker_buy_quote_volume` | `client.market_data.get_candles_last_days(connector, pair, days=7, interval="1m")` inside `run_code` | B |
| Book `bids`/`asks` rows `{price, amount}`, best first | `client.market_data.get_order_book(connector, pair)` | B |
| Funding rate | `client.market_data.get_funding_info` (perps) | B |
| Own fills, depth at posting, rest time | `manage_bots(action="status")` / executor history | B |
| Public trade tape (price, size, aggressor side) | exchange websocket — **not exposed by the Hummingbot API** | A |

Tier B is what you have. It gives exact aggressor-signed volume per bar (`2·taker_buy − volume`),
so VPIN and Kyle's λ are computable without a tape. κ and A need either a tape (`fit_tape_intensity`)
or your own fills (`fit_own_fills`); with neither, **κ and A are assumed and you may not deploy
beyond a probe size** until ≥ 300 own fills exist.

Paper venues: `*_paper_trade` connectors do not run controllers and the API refuses them for
market data. Take market data from the real venue and execute on a testnet connector.

---

## 2. Volume layer — estimators

```
sigma_ret   Rogers-Satchell:  s² = mean[ ln(H/C)ln(H/O) + ln(L/C)ln(L/O) ] / bar_s      (drift-robust)
            EWMA:             s² = Σ λ^k r_{t-k}² / Σ λ^k / bar_s,   λ = 0.94           (reacts in ~15 bars)
            use max(RS, EWMA) as the risk sigma; bars ≥ 5× tick to stay above microstructure noise
sigma_abs   = sigma_ret × mid

share_h     = mean quote volume in hour-of-week slot h / Σ_h(...)        (168 slots; the bathtub curve)
Vdot_t      = W_week × share_h / 3600                                    market quote volume per second
rho         = our quote volume / market quote volume   over the same window     (participation)
tau_liq     = |P| / ( rho_max × Vdot_t )          seconds to exit position P (quote) without exceeding rho_max
I(Q)        = Y × sigma_D × sqrt( Q / V_D )       fractional impact of executing Q; Y ≈ 0.8, recalibrate from
                                                    your own slippage; sigma_D, V_D = daily vol and daily quote volume
SV          = 2·taker_buy_quote − quote_volume;   imb = SV / quote_volume ∈ [-1, 1]
VPIN_n      = Σ|V_buy − V_sell| / Σ V   over the last n = 50 buckets;  p_vpin = percentile of VPIN in its 7-day history
Kyle λ      = Cov(r, SV) / Var(SV)    price move per unit signed quote volume (rising λ = thinning book)
Amihud      = mean(|r| / quote_volume)
```

VPIN here is computed on time bars with exact aggressor volume. It measures flow imbalance, not
the volume-clock quantity of the original paper; it is a regime flag, not a probability of loss.

**Fill intensity** (the engine of every later formula): `λ(δ) = A·e^{-κδ}` fills/s per side at depth δ
(price units from mid), per order of size `lot`.

```
tape:      δ_i = how far market order i reached beyond the pre-trade mid
           κ̂ = 1 / (mean δ_i − δ0)           se(κ̂) = κ̂/√n           Â = (n / window_s)·e^{κ̂ δ0}
own fills: per depth bin, λ̂ = Σ filled / Σ rest_seconds          (censored-exponential MLE)
           then weighted least squares  ln λ̂_bin = ln A − κ δ_bin
P(≥1 fill in dt) = 1 − e^{-λ dt}           (not λ·dt: the Bernoulli shortcut overstates by λdt/2)
```

## 3. Edge layer — is there anything to harvest?

```
markout_τ(fill)  = side × (mid_{t+τ} − fill_px) / fill_px × 1e4      bps; side=+1 for our buy, −1 for our sell; τ = 5-30 s
c  (per-fill cost, price units) = fee + E[adverse markout]  = (fee_bps + max(0, −mean markout_bps)) × 1e-4 × mid
net_edge_bps     = depth_bps − c_bps          edge_lcb = mean(net_edge) − 1.645 · sd / √n     (one-sided 95%)
risk-neutral optimum:    δ* = c + 1/κ ;      profit rate per side = (A/κ)·e^{-1-κc}  per base unit per second
```

**Gate: `edge_lcb_bps > 0`.** If not, no spread setting fixes it: stop or move venue/fee tier.
Maker fees above ~1 bp make most majors unquotable — check `c` before anything else.

## 4. Quote and inventory layer

Inventory `q` is signed base units; `lot` Δ is one order's size; `γ` is risk aversion per quote unit.

**Finite horizon (Avellaneda-Stoikov)** — use when there is a real deadline τ = T − t (session
end, funding timestamp, forced close). Lot-scaled form, verified against the numerical HJB:

```
reservation  r = S − q·γ·σ²·τ
spread       δ_bid + δ_ask = γ·σ²·τ·Δ + (2/(γΔ))·ln(1 + γΔ/κ)
validity     θτ = 2·Δ·λ0·κ·(γσ²τ)·τ ≤ 0.4   (λ0 = A·e^{-κ·half_spread}); relative error of the shift ≈ 0.25·θτ
```
Beyond that, trading has already pulled inventory back and A-S over-skews: use GLFT.
**Do not use the spread formula printed in the Stanford note** (`γσ²(T−t) + ln(1+γ/κ)`): it omits the
`2/γ` factor and is wrong by an order of magnitude (derivations.md).

**Stationary (GLFT)** — the default for 24/7 crypto, no terminal time, cost-aware:

```
A_eff = A·e^{-κc}
c1 = ln(1 + γΔ/κ)/(γΔ)
c2 = sqrt( γ/(2·A_eff·Δ·κ) · (1 + γΔ/κ)^{κ/(γΔ) + 1} )
skew       = σ·c2                              price per base unit of inventory
half       = c + c1 + ½·Δ·skew
bid_depth  = half + skew·q        ask_depth = half − skew·q          (from mid; floor both at c and ≥ 1 tick)
```
Valid while |q| is well inside the inventory bounds; it matched the numerical lattice to ~1 %.

**Calibrate γ from the inventory you will tolerate, not from taste.** Under linear skew `k̃` the
inventory is an Ornstein-Uhlenbeck process with stationary

```
std(q) = sqrt( Δ / (2·κ·k̃) )              (independent of A)
z·std(q) = q_max   →   γ  by bisection on the GLFT skew      (gamma_for_inventory_glft; z = 3)
unskewed:   E[time to hit ±q_max] = (q_max/Δ)² / (2·λ0)        — if this is minutes, you need skew, not luck
```

**Size taper (optional second control):** `bid_size = φ·e^{-η·max(q,0)}`, `ask_size = φ·e^{-η·max(−q,0)}`,
`η = ln2 / q*` with η > 0 (half size at q*).

**Spread capture vs one-sided fills** (quote both, wait N periods of dt for the other side):

```
p_b = 1 − e^{-λ_b dt},  p_a = 1 − e^{-λ_a dt}
P_spread     = [ p_b p_a + p_b(1−p_a)(1−(1−p_a)^N) + p_a(1−p_b)(1−(1−p_b)^N) ] / (p_b + p_a − p_b p_a)
P_one_sided  = 1 − P_spread          (each one-sided cycle leaves ±Δ of inventory)
```
Falling `P_spread` with rising VPIN is adverse selection showing up in the fills.

## 5. Tail and liquidity layer

```
horizon      τ_liq from §2 (the time you would actually need to exit), floor 1 bar
VaR_α        = |P|·z_α·σ_ret·√τ_liq                 z_.99 = 2.326
ES_α         = |P|·σ_ret·√τ_liq · φ(z_α)/(1−α)      (normal: 2.665·|P|σ√τ at 99 %)
fat tails    z_cf = z + (z²−1)s/6 + (z³−3z)k/24 − (2z³−5z)s²/36,   s = s1/√n, k = k1/n  (n = τ/bar_s steps)
             prefer historical ES on EWMA-filtered returns when ≥ 1000 bars exist
LVaR         = |P|·[ z σ_ret √τ + ½(s̄_rel + 2.33·sd_s) + I(P) ]       (spread + impact, Bangia et al.)
exit trajectory (Almgren-Chriss):  x(t) = X·sinh(k(T−t))/sinh(kT),  k = sqrt(λ_ra σ²/η_tmp);  λ_ra → 0 is TWAP
Kupiec POF   LR = −2 ln[(1−p)^{N−x} p^x] + 2 ln[(1−x/N)^{N−x}(x/N)^x] ~ χ²₁ ;  reject VaR model at p-value < 0.05
```
At $2.5k a major pair's `τ_liq` is seconds and never binds; on a thin pair it can be minutes. Compute
it anyway — it is the number that tells you a pair is too thin before you are in it.

## 6. Leverage, loss limits, sizing

**Leverage multiplies risk** (the IB guide's re-levering identity with zero tax, `β_eq = β_asset·(1 + D/E) = L·β_asset`):
equity-level vol is `L·σ`.

```
isolated long  P_liq = E·(1 − 1/L + m)          short  P_liq = E·(1 + 1/L − m)       m = maintenance margin rate
P(touch within T), b = ln(P_liq/E):   Φ((b − νT)/(σ√T)) + e^{2νb/σ²}·Φ((b + νT)/(σ√T))     ν = log-drift per s (0 → 2Φ(b/(σ√T)))
require P(touch within 24 h) ≤ 0.1 %; otherwise cut leverage — this gate cannot be fixed by scaling size
```

**Kill-switch calibration** — a loss limit that trips on noise is a bug; one that never trips is decoration.
With strategy PnL drift `μ` and vol `σ_p` (both per second; `σ_p = std(q)·σ_abs` for an inventory book):

```
start-based loss limit D (PnL touches −D):   P_hit(T) = Φ((−D−μT)/(σ_p√T)) + e^{-2μD/σ_p²}·Φ((−D+μT)/(σ_p√T))
                                             D_noise(p) = −σ_p²·ln(p)/(2μ)           (T → ∞, e.g. p = 5 %)
high-water-mark limit D (drawdown from peak): E[time to trip] = σ_p²/(2μ²)·(e^{2μD/σ_p²} − 1) − D/μ   (set ≥ 30 days)
```
Choose `D ≥ D_noise` so the switch does not fire on noise, and `D ≤` the loss you can actually
tolerate. If the first exceeds the second, **the edge-to-risk ratio is too thin to run**.
Use the result for `max_global_drawdown_quote` and `global_stop_loss`.

**Sizing:** fractional Kelly on the lower bound: `f = min(cap, 0.25·μ_lcb/σ²)`, `μ_lcb = μ̂ − z·σ/√N`.
**Across sleeves**, with weights `w` and covariance `Σ`: `σ_p = √(wᵀΣw)`, risk contribution
`RC_i = w_i(Σw)_i/σ_p` (sums to σ_p); trim the sleeve whose RC share exceeds its capital share.

**Market beta and carry (finance basics):**
```
β_i = Cov(r_i, r_BTC)/Var(r_BTC);   net beta exposure B = Σ q_i·S_i·β_i;   hedge notional = −B
CAPM hurdle on carried inventory: E[r_i] = r_f + β_i·MRP;   carry = r·|P|·H/(365·86400) for hold time H seconds
perp funding: carry per second = f_8h/28800 × notional   (you pay when long and f > 0)
equal-quote clips: average price paid = n / Σ(1/p_i)  — the harmonic mean, not the arithmetic mean
```

## 7. Triangular arbitrage overlay

Never detect on mid or on the note's `mat` alone — use executable rates.

```
leg rate:  buy base  → 1/ask ;   sell base → bid                  (units of the asset you end with per unit spent)
G_gross = Π r_i ;     G_net = Π r_i·(1 − f_i)  with depth-walked VWAP rates (vwap_buy / vwap_sell)
break-even equal fee   f* = 1 − G_gross^{-1/n}      (0 if G_gross ≤ 1)         e.g. note example: G = 1.0101 → f* = 33.3 bps/leg
N* = largest notional with G_net(VWAP at N) ≥ 1 + ε
unwind loss if leg k fails after k legs:   u_k = z·σ_ret·√L + half_spread + fee          L = leg latency (s)
EV = Π p_k · N·(G_net − 1) − Σ_k (Π_{j<k} p_j)(1 − p_k)·N·u_k
```
Trade only when `EV/N ≥` one tick of edge and `EV` stays positive with `p_k` cut by its standard error.
The note's two examples reproduce as G = 1.0101 and G = 0.9885 (the note rounds the loss to 1 %).
Real 1 % triangles do not persist; if you see one, suspect a stale quote before suspecting free money.

---

## 8. The gate — one number per risk, take the minimum

Every multiplier is in [0, 1]. Pure-scale gates (liquidity, tail) are exact because τ_liq and ES are
linear in position size.

| Gate | Multiplier | Default threshold (heuristic — tune, do not worship) |
|---|---|---|
| data | 1 if book uncrossed, `n ≥ 300`, quote age ≤ 5 s, else 0 | |
| edge | 1 if `edge_lcb_bps > 0`, else 0 | |
| toxicity | `clip((0.95 − p_vpin)/(0.95 − 0.80))` | flag > 0.80, halt ≥ 0.95 |
| inventory | `clip((1 − \|q\|/q_max)/(1 − 0.5))` | reduce-only beyond q_max |
| liquidity | `clip(τ_max / τ_liq)` | τ_max = 300 s |
| tail | `clip(β·C / ES_99)` | β = 5 % of sleeve capital C |
| liq price | 1 if `P(touch, 24 h) ≤ 0.1 %`, else 0 | perps only |
| drawdown | `clip((1 − DD/D)/(1 − 0.5))` | D from §6 |

`multiplier = min(gates)`; `state = HALT if 0, GO if ≥ 0.8, else THROTTLE`. Scale `lot` and
`total_amount_quote` by the multiplier; on HALT stop quoting through `manage_bots`. When the inventory
gate binds, tighten `max_base_pct` to make the book reduce-only on the loaded side.

```json
{"state": "THROTTLE", "multiplier": 0.62, "binding_gate": "toxicity",
 "gates": {"data": 1, "edge": 1, "toxicity": 0.62, "inventory": 1, "liquidity": 1, "tail": 1, "liq_price": 1, "drawdown": 1},
 "inputs": {"n_samples": 812, "sigma_ret_per_sqrt_s": 9.8e-5, "kappa": 2.5, "A": 0.05, "p_vpin": 0.89, "c_bps": 1.4},
 "quotes": {"half_spread_bps": 3.2, "skew_bps_per_lot": 0.3, "gamma": 0.074},
 "limits": {"q_max_base": 0.25, "max_base_pct": 0.6, "loss_limit_quote": 50},
 "provenance": {"kappa": "estimated, n=812, se=0.09", "A": "estimated", "Y": "assumed 0.8"},
 "next_review_s": 300}
```
Write the verdict to the agent journal (`trading_agent_journal_write`) each tick it changes state.

## 9. Mapping onto `pmm_mister`

`pmm_mister` is its own algorithm: it skews linearly between `min_base_pct` and `max_base_pct`
and places levels itself. Use this skill to choose its parameters, not to replace it.

| Derived | Parameter | Note |
|---|---|---|
| level 0 = `half / S`; deeper levels wider (design choice, e.g. ×1.5, ×2.25) with `amounts_pct` weighted to level 0 | `buy_spreads`, `sell_spreads` | spreads are fractions of price, not bps; `tolerance_scaling` scales order tolerances, not spreads |
| `Δ_quote = amounts_pct_i × total_amount_quote × portfolio_allocation` | level order size | must be ≥ exchange minimum notional |
| `q_max × S / total_amount_quote` | `max_base_pct` | `current_base_pct = |position quote| / total_amount_quote` (not the allocation) |
| inventory half-life from §4 | `executor_refresh_time`, cooldowns | refresh slower than the fill rate, not faster |
| D from §6 | `max_global_drawdown_quote`, `global_stop_loss` | confirm on a probe whether the switch is start- or peak-based |
| `c_bps` | `take_profit` | must exceed round-trip fee + adverse markout |

## 10. Worked example — hypothetical inputs, not live data

ETH-USDT sleeve `C = 1000` USDT, `S = 2400`, `σ_ret = 9.8e-5/√s` (σ_abs = 0.235 USDT/√s), `κ = 2.5/USDT`,
`A = 0.05/s`, `Δ = 0.05 ETH`, `c = 1.4 bp = 0.336 USDT`, `q_max = 0.25 ETH`, `z = 3`.

| Quantity | Result |
|---|---|
| γ (GLFT, from `3·std(q) = q_max`) | 0.074 USDT⁻¹ |
| half-spread / skew | 0.772 USDT (3.2 bp) / 1.44 USDT per ETH |
| std(q), 3·std(q) | 0.0833, 0.250 ETH |
| own fill rate at half-spread | 26 fills/h/side; unskewed exit of ±q_max in ≈ 29 min |
| net edge per fill | 0.436 USDT/ETH = 1.8 bp; μ ≈ 1.1 USDT/h |
| inventory PnL σ_p | 0.0196 USDT/√s = 1.18 USDT/√h |
| noise-implied loss limit (p = 5 %) | 1.8 USDT — a 50 USDT kill switch fires only on model failure |
| mean time to a 4.4 USDT peak drawdown | ≈ 30 days |
| `τ_liq` of the 600 USDT cap, ρ = 5 % | 0.5 s on a 15 bn/week pair; 346 s on a 21 m/week pair → liquidity multiplier 0.87 |
| 99 % VaR / ES, τ = 300 s | 2.37 / 2.71 USDT |
| 10× long liquidation | 2169.6; P(touch in 24 h) = 0.046 % |

## 11. Never

- Never quote on `κ` or `A` you did not measure or label as assumed, and never size beyond a probe on assumed ones.
- Never use a Gaussian VaR as the only tail number on a book that can gap; show ES and the CF figure.
- Never set a kill switch without computing `D_noise` — a switch inside the noise band churns the book.
- Never read a high VPIN as a price forecast; it is a reason to widen and shrink, not to flip direction.
- Never treat a triangle as free money before fees, depth, and latency.
- Never carry a position on a venue whose book fails the `data` gate to "wait it out".
- Never paste the whole kernel when three functions will do, and never do arithmetic in your head.
