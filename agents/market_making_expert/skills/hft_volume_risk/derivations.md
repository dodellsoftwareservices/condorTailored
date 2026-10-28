# derivations — provenance, errata and validation for `hft_volume_risk`

Read this when a number looks wrong, when you need to defend a formula, or before changing one.
`SKILL.md` is the playbook; this is the audit trail.

## 1. Where each formula comes from

**The three documents in `hedge/`** (read in full for this skill):

| Source | Used for |
|---|---|
| `stoikov-marketmakingmodel.pdf` (Fushimi, González Rojas, Herman, Stanford 2018, implementing Avellaneda-Stoikov 2008) | Poisson fill intensity `λ(δ)=Ae^{-κδ}`; indifference price `r = s − qγσ²(T−t)`; inventory-dependent order size; time-of-day "bathtub" intensity `α_t`; quote/wait/requote cycle and its Markov chain (spread capture vs one-sided fill) |
| `triangular-arbitrage-explained.md` | cycle product over bid/ask rates, i.e. `G = Π r_i`; the 3-asset detector; bid/ask spread as the market maker's margin |
| `400-IB-Questions-Interview-Guide.pdf` (164 pp.) | **Corporate-finance basics only** — CAPM `r = r_f + β·ERP`, beta un-/re-levering `β_L = β_U(1+(1−t)D/E)`, WACC, discounting, liquidity-crunch failure modes, the harmonic-mean "average speed" teaser. It contains no market-risk, VaR, or microstructure maths; it is used in §6 of the playbook for beta exposure, leverage scaling, carry, and clip cost basis, and nowhere else. |

**External literature, cited from memory** (not provided, not re-checked online in this session —
verify a citation before quoting it; the *formulas* are what was tested, see §4):

| Formula | Origin |
|---|---|
| A-S finite-horizon quotes (lot-scaled) | Avellaneda & Stoikov, "High-frequency trading in a limit order book", 2008 |
| GLFT stationary quotes, inventory bounds | Guéant, Lehalle & Fernandez-Tapia, 2013 (the Stanford note cites it) |
| Square-root impact `I = Yσ√(Q/V)` | Tóth et al. 2011; Bouchaud and co-authors; Y ≈ 0.5-1 empirically |
| VPIN, bulk volume classification | Easley, López de Prado & O'Hara, 2012 |
| Kyle's λ, Amihud illiquidity | Kyle 1985; Amihud 2002 |
| Liquidity-adjusted VaR | Bangia, Diebold, Schuermann & Stroughair, 1999 |
| Cornish-Fisher expansion | Cornish & Fisher 1937; Zangari 1996 for VaR use |
| Almgren-Chriss trajectory | Almgren & Chriss, 2000 |
| Kupiec proportion-of-failures test | Kupiec 1995 |
| Mean time to drawdown | Taylor 1975 / Karlin & Taylor, reflected Brownian motion |
| Barrier-touch probabilities | reflection principle for Brownian motion with drift |

## 2. Errata in the Stanford note (each one checked, not assumed)

1. **Spread formula.** The note prints `δ_a + δ_b = γσ²(T−t) + ln(1+γ/κ)`. The Avellaneda-Stoikov
   result is `γσ²(T−t) + (2/γ)·ln(1+γ/κ)`. For the parameters used in the verification
   (γ=0.1, σ=0.3, κ=1.5, τ=3 s) the note's value is 0.0915 and the numerical solution of the control
   problem gives 1.3177; the correct formula gives 1.3178. The note absorbs the discrepancy by
   re-calibrating to observed spreads, which hides it. This skill uses the correct form.
2. **HJB transitions.** The printed HJB writes `q+1` in both the bid and the ask term; the ask term
   is `q−1` (a sale reduces inventory).
3. **Order-size taper.** The note sets `η = −0.005` in `φ_bid = φ_max·e^{−ηq}` for `q>0`, which would
   *enlarge* the bid when already long. The taper only works with `η > 0`; its own Figure 1 slopes down.
4. **One-sided-fill probability.** The note's `q⋆ = p(0,1) + p(0,1)p(1,1)⁵p(1,0)` double-counts the first
   fill. With independent Poisson fills the exact conditional probabilities are in `spread_capture`
   and sum to one (§4 test).
5. **Fill probability.** The simulator uses a Bernoulli draw with probability `λΔ`; the exact
   probability of at least one Poisson arrival is `1 − e^{−λΔ}` (the shortcut overstates by `λΔ/2`).
6. **Zero-latency, no-impact, no-competition** are simulator assumptions, not features: they are why a
   real book needs `fit_own_fills` and a toxicity gate.

The note's *results tables* (e.g. AMZN P&L) come from a simulator with those assumptions; they are
not evidence about live crypto venues and are not used to set any threshold here.

## 3. Derivations that are not in any source above

**Lot-scaled A-S.** With CARA utility `u = −exp(−γ(x+qs+θ))` and fills of Δ base units at depth δ,
a bid fill changes the exponent by `Δδ + θ(q+Δ) − θ(q)`. Maximising `A e^{−κδ}(1 − a·e^{−γΔδ})/γ`
with `a = e^{−γ(θ(q+Δ)−θ(q))}` gives

```
δ_bid* = (1/(γΔ))·ln(1 + γΔ/κ) − (θ(q+Δ) − θ(q))/Δ        δ_ask* = (1/(γΔ))·ln(1 + γΔ/κ) − (θ(q−Δ) − θ(q))/Δ
```
and with `θ ≈ −½γσ²q²τ` the second difference yields `reservation = S − qγσ²τ` and
`spread = γσ²τΔ + (2/(γΔ))ln(1+γΔ/κ)`.

**GLFT with lot size.** Substituting `w_n = e^{κθ_n/Δ}` linearises the HJB:
`∂_τ w_n = η(w_{n+1}+w_{n−1}) − α n² w_n` with `α = κγσ²Δ/2`, `η = A(1+γΔ/κ)^{−1−κ/(γΔ)}`.
The dominant eigenvector is Gaussian, `w_n ∝ e^{−b n²/2}`, `b = √(α/η)`, giving the linear skew
`σ·c2` per base unit and half-spread `c1 + ½Δσc2` quoted in the playbook. This was derived from the
lot-size HJB above, not transcribed from the paper's expression, and it matches the numerical lattice
for Δ = 1 and Δ = 2; check the lot-size dependence against the paper before relying on Δ ≠ 1 elsewhere.

**Per-fill cost.** A cost `c` per fill (fee plus expected adverse markout) enters as `A e^{−κδ}(δ−c)`.
With `δ = c + δ'` this is `A e^{−κc}·e^{−κδ'}·δ'`: the cost-free problem with `A_eff = A e^{−κc}`.
Hence `glft(..., cost=c) = c + glft(A_eff)`, exactly, and `δ* = c + 1/κ` for the risk-neutral case.

**Stationary inventory.** Under skew `k̃` per base unit the net drift of `q` is
`−2Δ·λ0·sinh(κk̃q) ≈ −2Δλ0κk̃·q`, the variance rate is `2Δ²λ0`, so
`Var(q) = 2Δ²λ0 / (2·2Δλ0κk̃) = Δ/(2κk̃)`, independent of A. Unskewed, the walk's expected exit time
from ±q_max is `(q_max/Δ)²/(2λ0)`.

**Kill switch.** For PnL `dX = μdt + σ_p dW` the probability of ever touching −D is `e^{−2μD/σ_p²}`
(the stationary tail of the reflected drawdown at a *fixed* time has the same form but is **not** the
probability that a drawdown ever occurs, which tends to 1 as T → ∞). From the peak, the expected time to a drawdown D
solves `½σ²u'' − μu' = −1`, `u'(0)=0`, `u(D)=0` ⇒ `u(0) = σ²/(2μ²)(e^{2μD/σ²}−1) − D/μ`.

**Finance-basics bridge.**
`β_L = β_U(1 + (1−t)D/E)` with `t = 0` is the margin-leverage multiplier `L = 1 + D/E`: a perp at
leverage L has `L×` the equity-level beta and volatility of the unlevered position. CAPM's
`r_f + β·ERP` is the hurdle a carried inventory must beat. A solvent book can still fail on
liquidity — the guide's distressed-company "liquidity crunch" — which is what `τ_liq` measures.
"Average speed over equal distances is the harmonic mean" is the same identity as "average price over
equal-quote clips is the harmonic mean of the prices".

## 4. Validation record

`tests/test_hft_volume_risk_kernel.py` loads the code block of `risk_kernel.md` and checks 90+
properties, among them:

| Claim | Checked against |
|---|---|
| A-S spread and reservation shift (lot 1 and 2) | numerical solution of the lot-size HJB on an inventory lattice; reservation error stays within `0.3·θτ` |
| GLFT depths (lot 1 and 2, q = 0..6) | same lattice, stationary, within 4 % |
| Per-fill-cost shift | brute-force argmax of the per-side objective |
| Stationary inventory std; symmetric exit time | Gillespie simulation of the fill process |
| κ MLE, own-fill intensity fit | simulated exponential penetration and censored waits |
| Spread-capture probabilities | Monte Carlo of the quote-wait-cancel cycle |
| Normal VaR/ES, Kupiec p-value, Cornish-Fisher | Monte Carlo, `scipy.stats.chi2`, Gamma quantile |
| Barrier-touch (with drift), loss-hit probability, mean time to drawdown | path simulation (with the 0.5826σ√dt overshoot correction for discrete monitoring) |
| Triangle examples from the note | G = 1.0101 and 0.9885; break-even fee zeroes the edge |
| Gate multipliers | exact scale identities |

What is **not** validated: that any of this has positive expectancy on a live venue. It checks that the
formulas implement the models correctly, not that the models describe your market. The A-S/GLFT family
assumes Brownian mid-price, constant `A` and `κ`, no jumps, no competition; crypto breaks all four at
times, which is why the toxicity, tail and kill-switch gates exist. Treat every default threshold as a
starting point and recalibrate from your own fills.
