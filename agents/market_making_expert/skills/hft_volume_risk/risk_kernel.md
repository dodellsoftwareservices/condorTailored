# risk_kernel — the tested formula library for `hft_volume_risk`

Pure Python, numpy + stdlib only, so it runs inside `run_code`. It is verified
against a numerical Hamilton-Jacobi-Bellman solution, Monte Carlo simulation and
closed-form cross-checks by `tests/test_hft_volume_risk_kernel.py`, which loads
the block below straight from this file — edit the code here and the tests run
against exactly what agents paste.

**How to use it**

1. Read it once per session: `manage_skill(action="read_file", name="hft_volume_risk", file="risk_kernel.md")`.
2. Paste the three import lines and constants at the top plus only the functions the
   step needs into one `run_code` snippet. Every function is independent of the
   others except where a docstring says otherwise (`glft` is used by
   `gamma_for_inventory_glft`; `p_fill` by `spread_capture`; `dd_mean_time` by
   `dd_limit_for_time`; `hist_var_es` needs nothing).
3. Print every intermediate number with its unit. `print` is the output.
4. Run the same snippet a third time? Promote it to a routine
   (`manage_routines(action="create_routine")`) instead of pasting it again.

```python
"""hft_volume_risk kernel. numpy + stdlib only (safe inside run_code).

Units, everywhere: price and money in the quote currency (USDT), size in base
units, time in seconds, volatility "sigma" in price units per sqrt(second)
(`sigma_abs`) or return units per sqrt(second) (`sigma_ret`), whichever the
argument name says. Never mix the two: sigma_abs = sigma_ret * mid.
"""
import math
from statistics import NormalDist

import numpy as np

_N = NormalDist()
Phi, phi, zinv = _N.cdf, _N.pdf, _N.inv_cdf


# ---------------------------------------------------------------- 1. volatility
def sigma_rs(o, h, l, c, bar_s):
    """Rogers-Satchell vol per sqrt(second), drift-robust, from OHLC bars."""
    o, h, l, c = (np.asarray(x, float) for x in (o, h, l, c))
    rs = np.log(h / c) * np.log(h / o) + np.log(l / c) * np.log(l / o)
    return math.sqrt(max(rs.mean(), 0.0) / bar_s)


def sigma_ewma(log_returns, bar_s, lam=0.94):
    """EWMA vol per sqrt(second); the newest return is the last element."""
    r = np.asarray(log_returns, float)
    w = lam ** np.arange(len(r) - 1, -1, -1)
    return math.sqrt((w * r * r).sum() / w.sum() / bar_s)


# ------------------------------------------------------------------ 2. volume
def hour_of_week_share(slot, quote_vol):
    """Share of weekly quote volume in each of the 168 hour-of-week slots."""
    slot, v = np.asarray(slot), np.asarray(quote_vol, float)
    m = np.array([v[slot == k].mean() if (slot == k).any() else 0.0 for k in range(168)])
    return m / m.sum()


def volume_rate(weekly_quote_volume, share_slot, slot_s=3600.0):
    """Expected market quote volume per second inside a slot."""
    return weekly_quote_volume * share_slot / slot_s


def participation(our_quote_volume, market_quote_volume):
    return our_quote_volume / market_quote_volume


def liquidation_seconds(position_quote, vol_rate_s, rho_max):
    """Time to exit a position while never exceeding participation rho_max."""
    return abs(position_quote) / (rho_max * vol_rate_s)


def sqrt_impact(order_quote, daily_quote_volume, sigma_daily_ret, Y=0.8):
    """Square-root law: fractional price impact of executing `order_quote`."""
    return Y * sigma_daily_ret * math.sqrt(abs(order_quote) / daily_quote_volume)


def signed_flow(quote_vol, taker_buy_quote):
    """Aggressor-signed volume and imbalance in [-1, 1] straight from candles."""
    qv, tb = np.asarray(quote_vol, float), np.asarray(taker_buy_quote, float)
    sv = 2.0 * tb - qv
    return sv, np.divide(sv, qv, out=np.zeros_like(sv), where=qv > 0)


def kyle_lambda(returns, signed_volume):
    """Price move per unit signed quote volume (OLS slope through the origin-centred data)."""
    r, s = np.asarray(returns, float), np.asarray(signed_volume, float)
    return float(np.cov(r, s)[0, 1] / np.var(s, ddof=1))


def amihud(returns, quote_vol):
    r, v = np.asarray(returns, float), np.asarray(quote_vol, float)
    return float(np.mean(np.abs(r) / v))


# ------------------------------------------------- 3. fill intensity and toxicity
def fit_tape_intensity(penetration, window_s, delta0=0.0):
    """lambda(d) = A exp(-kappa d) from a trade tape. `penetration` is how far
    from the pre-trade mid each market order reached (price units, per side)."""
    d = np.asarray(penetration, float)
    d = d[d >= delta0]
    n = len(d)
    kappa = 1.0 / (d.mean() - delta0)
    return dict(A=(n / window_s) * math.exp(kappa * delta0), kappa=kappa,
                se_kappa=kappa / math.sqrt(n), n=n)


def fit_own_fills(depth, rest_s, filled, bins=6):
    """lambda(d) from our own resting orders (right-censored exponential MLE).
    depth: quote depth at posting; rest_s: seconds it rested; filled: 0/1."""
    d, t, f = (np.asarray(x, float) for x in (depth, rest_s, filled))
    edges = np.quantile(d, np.linspace(0, 1, bins + 1))
    xs, ys, ws = [], [], []
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (d >= lo) & (d <= hi if hi == edges[-1] else d < hi)
        if f[m].sum() >= 5:
            xs.append(d[m].mean()); ys.append(math.log(f[m].sum() / t[m].sum())); ws.append(f[m].sum())
    slope, icpt = np.polyfit(xs, ys, 1, w=np.sqrt(ws))
    return dict(A=math.exp(icpt), kappa=-slope, n_bins=len(xs))


def p_fill(lam, dt):
    """P(at least one fill in dt) for a Poisson rate lam (exact, not lam*dt)."""
    return -math.expm1(-lam * dt)


def volume_buckets(price, size_quote, V):
    """Equal-volume buckets of V quote units -> (price change, volume) per bucket."""
    dP, vols, first, acc = [], [], None, 0.0
    for p, s in zip(price, size_quote):
        if first is None:
            first = p
        while s > 0:
            take = min(s, V - acc)
            acc += take; s -= take
            if acc >= V - 1e-12:
                dP.append(p - first); vols.append(acc); first, acc = None, 0.0
                if s > 0:
                    first = p
    return np.array(dP), np.array(vols)


def bvc_buy_fraction(dP, sigma_dP):
    """Bulk volume classification: P(buy) = Phi(dP / sigma_dP)."""
    return np.array([Phi(x / sigma_dP) for x in np.asarray(dP, float)])


def vpin(buy_vol, sell_vol, n):
    """Rolling VPIN over n buckets = sum|Vb - Vs| / sum V."""
    b, s = np.asarray(buy_vol, float), np.asarray(sell_vol, float)
    num, den = np.abs(b - s), b + s
    k = np.ones(n)
    return np.convolve(num, k, "valid") / np.convolve(den, k, "valid")


def percentile_rank(x, history):
    return float((np.asarray(history) <= x).mean())


def markout_bps(side, fill_px, mid_later):
    """side=+1 for our buy, -1 for our sell. Negative = adverse selection."""
    return 1e4 * side * (mid_later - fill_px) / fill_px


def edge_lcb(net_edge_bps, z=1.645):
    """One-sided lower confidence bound on mean edge per fill (bps)."""
    e = np.asarray(net_edge_bps, float)
    return float(e.mean() - z * e.std(ddof=1) / math.sqrt(len(e)))


def depth_opt(kappa, cost):
    """argmax_d A e^{-kappa d} (d - cost) = cost + 1/kappa."""
    return cost + 1.0 / kappa


def profit_rate(A, kappa, cost):
    """Max of A e^{-kappa d}(d - cost): per side, per second, per base unit."""
    return A / kappa * math.exp(-1.0 - kappa * cost)


# ---------------------------------------------------------------- 4. quoting
def avellaneda_stoikov(mid, q, gamma, sigma_abs, kappa, tau, lot=1.0):
    """Finite horizon. tau = T - t (s). q in base units. lot = order size (base).
    Spread uses (2/(gamma*lot)) ln(1+gamma*lot/kappa) -- NOT the Stanford note's
    ln(1+gamma/kappa), which drops the 2/gamma factor."""
    r = mid - q * gamma * sigma_abs**2 * tau
    spread = gamma * sigma_abs**2 * tau * lot + (2.0 / (gamma * lot)) * math.log1p(gamma * lot / kappa)
    return dict(reservation=r, spread=spread, bid=r - spread / 2, ask=r + spread / 2)


def as_validity(gamma, sigma_abs, A, kappa, half_spread, tau, lot=1.0):
    """tau * theta, theta = 2 lot lambda0 kappa (gamma sigma^2 tau) = inventory relaxation
    rate under the A-S skew. The A-S reservation shift is first order: its relative
    error is about 0.25 * this number. Use A-S while it is <= 0.4, GLFT beyond."""
    lam0 = A * math.exp(-kappa * half_spread)
    return 2.0 * lot * lam0 * kappa * gamma * sigma_abs**2 * tau**2


def glft(q, gamma, sigma_abs, A, kappa, lot=1.0, cost=0.0):
    """Stationary (T -> inf) Gueant-Lehalle-Fernandez-Tapia quotes, linear-in-q regime
    (|q| well inside the inventory bounds). Depths are measured from the mid, price units.
    A = market-order rate at zero depth for ONE side (1/s); each order is `lot` base units.
    cost = per-fill cost in price units (fee + expected adverse markout). Substituting
    depth = cost + d' turns the problem into the cost-free one with A_eff = A e^{-kappa cost},
    so the result is cost + GLFT(A_eff). Checked against the numerical HJB solution."""
    A_eff = A * math.exp(-kappa * cost)
    g = gamma * lot
    c1 = math.log1p(g / kappa) / g
    c2 = math.sqrt(gamma / (2 * A_eff * lot * kappa) * (1 + g / kappa) ** (kappa / g + 1))
    skew = sigma_abs * c2                            # price per base unit of inventory
    half = cost + c1 + 0.5 * lot * skew
    return dict(bid_depth=half + skew * q, ask_depth=half - skew * q,
                half_spread=half, skew_per_base=skew, A_eff=A_eff)


def inv_std_stationary(lot, kappa, skew_per_base):
    """Std of inventory under linear skew k~ (price per base unit of inventory)."""
    return math.sqrt(lot / (2.0 * kappa * skew_per_base))


def gamma_for_inventory(q_max, z, lot, kappa, sigma_abs, tau):
    """Smallest A-S gamma with z * std(q) <= q_max."""
    return lot * z**2 / (2.0 * kappa * sigma_abs**2 * tau * q_max**2)


def gamma_for_inventory_glft(q_max, z, lot, kappa, sigma_abs, A, cost=0.0):
    """gamma such that z * std(q) = q_max under the stationary GLFT skew (bisection;
    the skew is increasing in gamma). Use this for 24/7 markets."""
    def std(g):
        return inv_std_stationary(lot, kappa, glft(0.0, g, sigma_abs, A, kappa, lot, cost)["skew_per_base"])
    lo, hi = 1e-9, 1.0
    while z * std(hi) > q_max:
        hi *= 2
    for _ in range(200):
        mid = math.sqrt(lo * hi)
        lo, hi = (mid, hi) if z * std(mid) > q_max else (lo, mid)
    return hi


def size_taper(q, phi_max, eta):
    """Order sizes (bid, ask) that shrink on the side that adds to inventory.
    eta > 0 (half-size at q* = ln2/eta). The Stanford note prints eta = -0.005, which
    with exp(-eta q) would GROW the size on the loaded side."""
    return phi_max * math.exp(-eta * max(q, 0.0)), phi_max * math.exp(-eta * max(-q, 0.0))


def spread_capture(lam_bid, lam_ask, dt, wait_periods):
    """Quote both sides; if only one fills, wait `wait_periods` of length dt for the
    other, then cancel. Probabilities are conditional on leaving the 'quoting' state
    and sum to one: P_spread (both filled), P_one_sided (cancelled with inventory)."""
    pb, pa = p_fill(lam_bid, dt), p_fill(lam_ask, dt)
    both, b_first, a_first = pb * pa, pb * (1 - pa), pa * (1 - pb)
    z = both + b_first + a_first
    b_done = b_first * (1 - (1 - pa) ** wait_periods)
    a_done = a_first * (1 - (1 - pb) ** wait_periods)
    return dict(P_spread=(both + b_done + a_done) / z,
                P_one_sided=(b_first * (1 - pa) ** wait_periods + a_first * (1 - pb) ** wait_periods) / z)


def exit_time_symmetric(q_max, lot, lam_side):
    """E[time] for an unskewed inventory walk to first hit +-q_max."""
    return (q_max / lot) ** 2 / (2.0 * lam_side)


# ------------------------------------------------------------------ 5. tail risk
def var_es_normal(pos_quote, sigma_ret, horizon_s, alpha=0.99):
    s, z = sigma_ret * math.sqrt(horizon_s), zinv(alpha)
    return dict(var=abs(pos_quote) * z * s, es=abs(pos_quote) * s * phi(z) / (1 - alpha))


def agg_moments(skew1, exkurt1, n):
    """Skew and excess kurtosis of an n-step sum of iid steps."""
    return skew1 / math.sqrt(n), exkurt1 / n


def cornish_fisher_z(alpha, skew, exkurt):
    z = zinv(alpha)
    return z + (z * z - 1) * skew / 6 + (z**3 - 3 * z) * exkurt / 24 - (2 * z**3 - 5 * z) * skew**2 / 36


def hist_var_es(returns, pos_quote, alpha=0.99):
    loss = -np.sort(np.asarray(returns, float)) * abs(pos_quote)
    loss = np.sort(loss)[::-1]
    k = max(int(math.ceil((1 - alpha) * len(loss))), 1)
    return dict(var=float(loss[k - 1]), es=float(loss[:k].mean()))


def lvar(pos_quote, sigma_ret, horizon_s, spread_mean_rel, spread_sd_rel, impact_rel, alpha=0.99, z_s=2.33):
    """Bangia et al. exogenous + endogenous liquidity add-on."""
    z = zinv(alpha)
    return abs(pos_quote) * (z * sigma_ret * math.sqrt(horizon_s)
                             + 0.5 * (spread_mean_rel + z_s * spread_sd_rel) + impact_rel)


def kupiec(n, x, p):
    """POF test of VaR exceedances: x breaches in n days against level p=1-alpha."""
    pi = x / n
    ll0 = (n - x) * math.log1p(-p) + (x * math.log(p) if x else 0.0)
    ll1 = ((n - x) * math.log1p(-pi) if x < n else 0.0) + (x * math.log(pi) if x else 0.0)
    lr = 2 * (ll1 - ll0)
    return dict(lr=lr, p_value=math.erfc(math.sqrt(max(lr, 0.0) / 2)))


def ac_trajectory(X, T, sigma_abs, eta, lam, n=10):
    """Almgren-Chriss holdings x(t_k); eta = temporary impact (price per unit/second),
    lam = risk aversion. lam -> 0 recovers TWAP."""
    k = math.sqrt(lam * sigma_abs**2 / eta)
    t = np.linspace(0, T, n + 1)
    return X * np.sinh(k * (T - t)) / np.sinh(k * T) if k > 0 else X * (1 - t / T)


# --------------------------------------------------------- 6. leverage and drawdown
def liq_price(entry, lev, mmr, side):
    """Isolated margin, fees ignored. side=+1 long, -1 short."""
    return entry * (1 - side * (1 / lev - mmr))


def p_touch(entry, barrier, sigma_ret, horizon_s, nu=0.0):
    """P(price touches `barrier` within horizon). nu = log-drift per second."""
    sd = sigma_ret * math.sqrt(horizon_s)
    if barrier < entry:
        b = math.log(barrier / entry)
        return Phi((b - nu * horizon_s) / sd) + math.exp(2 * nu * b / sigma_ret**2) * Phi((b + nu * horizon_s) / sd)
    d = math.log(barrier / entry)
    return Phi((-d + nu * horizon_s) / sd) + math.exp(2 * nu * d / sigma_ret**2) * Phi((-d - nu * horizon_s) / sd)


def loss_hit_prob(mu, sigma, D, T=math.inf):
    """P(cumulative PnL ever touches -D within T), arithmetic BM, drift mu, vol sigma
    (per the same time unit as T). T=inf gives exp(-2 mu D / sigma^2) for mu>0.
    Use for a kill switch that measures loss from the START of the session."""
    if T == math.inf:
        return math.exp(-2.0 * mu * D / sigma**2) if mu > 0 else 1.0
    sd = sigma * math.sqrt(T)
    return Phi((-D - mu * T) / sd) + math.exp(-2.0 * mu * D / sigma**2) * Phi((-D + mu * T) / sd)


def loss_limit(mu, sigma, p_false):
    """Loss limit D with P(ever touched) = p_false (start-based kill switch, T=inf)."""
    return -sigma**2 * math.log(p_false) / (2.0 * mu)


def dd_mean_time(mu, sigma, D):
    """E[time until the drawdown from the running peak first reaches D], mu>0
    (Taylor 1975): (sigma^2/(2 mu^2))(exp(2 mu D/sigma^2) - 1) - D/mu.
    Use for a kill switch that measures loss from the HIGH-WATER MARK."""
    return sigma**2 / (2 * mu**2) * math.expm1(2 * mu * D / sigma**2) - D / mu


def dd_limit_for_time(mu, sigma, T_target):
    """Smallest D whose expected false-trigger time is at least T_target (bisection)."""
    lo, hi = 1e-12, sigma**2 / (2 * mu) * 60
    while dd_mean_time(mu, sigma, hi) < T_target:
        hi *= 2
    for _ in range(200):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if dd_mean_time(mu, sigma, mid) < T_target else (lo, mid)
    return hi


def kelly(mu_lcb, var, frac=0.25, cap=1.0):
    return min(cap, max(0.0, frac * mu_lcb / var))


def risk_contrib(w, cov):
    w, cov = np.asarray(w, float), np.asarray(cov, float)
    m = cov @ w
    sig = math.sqrt(float(w @ m))
    return w * m / sig, sig


def beta(r_asset, r_mkt):
    c = np.cov(r_asset, r_mkt)
    return float(c[0, 1] / c[1, 1])


def capm(rf, b, mrp):
    return rf + b * mrp


def relever_beta(unlevered, tax, debt_to_equity):
    """IB-guide formula. Trading use: tax=0, so beta_equity = beta_asset * (1 + D/E) = L * beta_asset."""
    return unlevered * (1 + (1 - tax) * debt_to_equity)


# ------------------------------------------------------------------ 7. arbitrage
def leg_rate(side, bid, ask):
    """Units of the asset you end with per unit you spend. buy base: 1/ask; sell base: bid."""
    return 1.0 / ask if side == "buy" else bid


def cycle_gross(rates):
    return float(np.prod(rates))


def cycle_net(rates, fees):
    return float(np.prod(np.asarray(rates) * (1 - np.asarray(fees))))


def breakeven_fee(rates):
    """Largest equal per-leg fee at which the cycle still breaks even."""
    G = cycle_gross(rates)
    return max(0.0, 1.0 - G ** (-1.0 / len(rates)))


def vwap_buy(asks, quote_budget):
    """asks: [(price, base_size)] best first. Returns (avg_price, base_bought, quote_spent)."""
    spent = base = 0.0
    for px, sz in asks:
        take = min(sz, (quote_budget - spent) / px)
        base += take; spent += take * px
        if spent >= quote_budget - 1e-12:
            break
    return (spent / base if base else float("nan")), base, spent


def vwap_sell(bids, base_qty):
    got = sold = 0.0
    for px, sz in bids:
        take = min(sz, base_qty - sold)
        sold += take; got += take * px
        if sold >= base_qty - 1e-12:
            break
    return (got / sold if sold else float("nan")), sold, got


def arb_ev(notional, G_net, p_leg, unwind_frac):
    """EV of a multi-leg cycle with leg-failure risk.
    p_leg[k]: P(leg k completes). unwind_frac[k]: loss fraction of notional if leg k
    fails after k legs have completed (0 for k=0)."""
    ev = float(np.prod(p_leg)) * notional * (G_net - 1.0)
    reach = 1.0
    for p, u in zip(p_leg, unwind_frac):
        ev -= reach * (1 - p) * notional * u
        reach *= p
    return ev


def unwind_frac(sigma_ret, latency_s, half_spread_rel, fee, z=2.33):
    return z * sigma_ret * math.sqrt(latency_s) + half_spread_rel + fee


# ------------------------------------------------------------------ 8. the gate
DEFAULTS = dict(
    min_samples=300, max_stale_s=5.0, tox_start=0.80, tox_halt=0.95, inv_start=0.50,
    tau_liq_max_s=300.0, es_budget_frac=0.05, p_liq_max=0.001, dd_start=0.50,
)


def assess(*, data_ok, n_samples, stale_s, edge_lcb_bps, vpin_pct, inv_abs, inv_max,
           tau_liq_s, es_quote, capital, p_liq, dd_now, dd_limit_quote, cfg=None):
    """Return per-gate multipliers in [0,1] and the binding gate. Every multiplier
    that is a pure scale (liquidity, ES) is exact because both quantities are linear
    in position size."""
    c = {**DEFAULTS, **(cfg or {})}
    clip = lambda x: float(min(1.0, max(0.0, x)))
    g = {
        "data": 1.0 if (data_ok and n_samples >= c["min_samples"] and stale_s <= c["max_stale_s"]) else 0.0,
        "edge": 1.0 if edge_lcb_bps > 0 else 0.0,
        "toxicity": clip((c["tox_halt"] - vpin_pct) / (c["tox_halt"] - c["tox_start"])),
        "inventory": clip((1 - inv_abs / inv_max) / (1 - c["inv_start"])),
        "liquidity": clip(c["tau_liq_max_s"] / tau_liq_s) if tau_liq_s > 0 else 1.0,
        "tail": clip(c["es_budget_frac"] * capital / es_quote) if es_quote > 0 else 1.0,
        "liq_price": 1.0 if p_liq <= c["p_liq_max"] else 0.0,
        "drawdown": clip((1 - dd_now / dd_limit_quote) / (1 - c["dd_start"])),
    }
    m = min(g.values())
    bind = min(g, key=g.get) if m < 1.0 else None
    state = "HALT" if m == 0 else ("GO" if m >= 0.8 else "THROTTLE")
    return dict(gates=g, multiplier=m, binding=bind, state=state)
```
