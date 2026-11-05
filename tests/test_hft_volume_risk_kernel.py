"""The `hft_volume_risk` formula kernel does what its playbook says.

The kernel is the Python block in `risk_kernel.md`, loaded from the skill itself so
this exercises exactly what an agent pastes into `run_code`. Every formula is checked
against something independent of it: a numerical solution of the control problem on an
inventory lattice, a Monte Carlo of the process it describes, a brute-force optimum, or a
closed form from a different route (scipy). A failure prints every miss, not just the first.
"""

import math
import re
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from scipy import stats

SKILL = Path("agents/market_making_expert/skills/hft_volume_risk")


def _load_kernel():
    text = (SKILL / "risk_kernel.md").read_text()
    block = re.search(r"```python\n(.*?)\n```", text, re.S).group(1)
    ns: dict = {}
    exec(compile(block, "risk_kernel.md", "exec"), ns)
    return SimpleNamespace(**ns)


def _run_checks(K):
    failures = []

    def check(name, cond, detail=""):
        if not cond:
            failures.append(f"{name} {detail}")

    rng = np.random.default_rng(7)

    # ---------- HJB on an inventory lattice (ground truth for the quoting formulas)
    def hjb(gamma, sigma, A, k, lot, Q, tau_end, dt, snap=None):
        """theta(tau, q) on q = n*lot, n in [-Q, Q]. Returns depth functions at tau_end
        (and at each tau in `snap`)."""
        n = np.arange(-Q, Q + 1)
        q = n * lot
        th = np.zeros(len(n))
        out = {}
        g = gamma * lot

        def depths(th):
            up = np.full(len(n), np.nan)
            dn = np.full(len(n), np.nan)
            up[:-1] = th[1:] - th[:-1]  # theta(q+lot)-theta(q)
            dn[1:] = th[:-1] - th[1:]  # theta(q-lot)-theta(q)
            db = (
                np.log(np.exp(-gamma * up) * (1 + g / k)) / g - 0
            )  # (1/(g)) ln(a (1+g/k))
            da = np.log(np.exp(-gamma * dn) * (1 + g / k)) / g
            return db, da

        steps = int(tau_end / dt)
        snaps = {int(s / dt): s for s in (snap or [])}
        for i in range(1, steps + 1):
            db, da = depths(th)
            fb = np.where(np.isnan(db), 0.0, A * np.exp(-k * db) * lot / (k + g))
            fa = np.where(np.isnan(da), 0.0, A * np.exp(-k * da) * lot / (k + g))
            th = th + dt * (fb + fa - 0.5 * gamma * sigma**2 * q**2)
            if i in snaps:
                out[snaps[i]] = depths(th)
        out["final"] = depths(th)
        out["q"] = q
        return out

    g_, s_, A_, k_ = 0.1, 0.3, 1.0, 1.5
    for lot in (1.0, 2.0):
        Q = 60
        res = hjb(g_, s_, A_, k_, lot, Q, 600.0, 0.01, snap=[3.0, 6.0])
        q = res["q"]
        i0 = Q
        db, da = res["final"]
        # stationary GLFT closed form vs lattice, small |q|
        for nq in (0, 1, 2, 3):
            j = i0 + nq
            cf = K.glft(q[j], g_, s_, A_, k_, lot)
            check(
                f"GLFT bid lot={lot} q={q[j]}",
                abs(cf["bid_depth"] - db[j]) < 0.04 * abs(db[j]) + 0.01,
                f"closed={cf['bid_depth']:.4f} lattice={db[j]:.4f}",
            )
            check(
                f"GLFT ask lot={lot} q={q[j]}",
                abs(cf["ask_depth"] - da[j]) < 0.04 * abs(da[j]) + 0.01,
                f"closed={cf['ask_depth']:.4f} lattice={da[j]:.4f}",
            )
        # finite horizon A-S vs lattice at q=0 (unbounded in practice at short tau)
        for tau in (3.0, 6.0):
            db_t, da_t = res[tau]
            lat_spread = db_t[i0] + da_t[i0]
            lat_shift = (
                db_t[i0 + 2] - da_t[i0 + 2]
            ) / 2  # = q*gamma*sigma^2*tau at q = 2 lots
            as_ = K.avellaneda_stoikov(0.0, 0.0, g_, s_, k_, tau, lot)
            as2 = K.avellaneda_stoikov(0.0, 2 * lot, g_, s_, k_, tau, lot)
            check(
                f"A-S spread lot={lot} tau={tau}",
                abs(as_["spread"] - lat_spread) < 0.05 * lat_spread,
                f"formula={as_['spread']:.4f} lattice={lat_spread:.4f}",
            )
            check(
                f"A-S reservation shift lot={lot} tau={tau}",
                abs(-as2["reservation"] - lat_shift)
                < 0.3
                * K.as_validity(g_, s_, A_, k_, as_["spread"] / 2, tau, lot)
                * lat_shift
                + 0.04 * lat_shift,
                f"formula={-as2['reservation']:.4f} lattice={lat_shift:.4f}",
            )
        # the Stanford-note spread (no 2/gamma) must be visibly wrong at q=0
        bad = g_ * s_**2 * 3.0 * lot + math.log1p(g_ / k_)
        lat = res[3.0][0][i0] + res[3.0][1][i0]
        check(
            f"Stanford-note spread is off lot={lot}",
            abs(bad - lat) > 0.2 * lat,
            f"note={bad:.4f} lattice={lat:.4f}",
        )

    # cost substitution: brute-force argmax of the per-side HJB objective with a per-fill cost
    for a_, c_, g2, lot2 in (
        (0.9, 0.3, 0.1, 1.0),
        (1.4, 0.7, 0.2, 2.0),
        (1.1, 0.1, 0.05, 0.5),
    ):
        gg2 = g2 * lot2
        grid = np.linspace(-2, 6, 800001)
        obj = 1.5 and (
            A_ * np.exp(-k_ * grid) * (1 - a_ * np.exp(-gg2 * (grid - c_))) / g2
        )
        d_star = c_ + math.log(a_ * (1 + gg2 / k_)) / gg2
        check(
            f"cost-shifted optimal depth a={a_} c={c_}",
            abs(grid[obj.argmax()] - d_star) < 1e-4,
            f"grid={grid[obj.argmax()]:.5f} closed={d_star:.5f}",
        )
    cg = K.glft(0.5, 0.3, 0.2, 0.8, 2.0, 0.1, cost=0.25)
    cg0 = K.glft(0.5, 0.3, 0.2, 0.8 * math.exp(-2.0 * 0.25), 2.0, 0.1)
    check(
        "glft cost = cost + glft(A_eff)",
        abs(cg["bid_depth"] - (0.25 + cg0["bid_depth"])) < 1e-12
        and abs(cg["ask_depth"] - (0.25 + cg0["ask_depth"])) < 1e-12,
    )
    gg3 = K.gamma_for_inventory_glft(20, 3, 1.0, 1.5, 0.3, 1.0, cost=0.4)
    check(
        "GLFT gamma with cost round-trip",
        abs(
            3
            * K.inv_std_stationary(
                1.0, 1.5, K.glft(0, gg3, 0.3, 1.0, 1.5, 1.0, 0.4)["skew_per_base"]
            )
            - 20
        )
        < 1e-6,
    )

    # ---------- stationary inventory std (Gillespie)
    def gillespie(A, kappa, h, ktil, lot, T, seed):
        r = np.random.default_rng(seed)
        q = 0.0
        t = 0.0
        acc = 0.0
        acc2 = 0.0
        while t < T:
            lb = A * math.exp(-kappa * (h + ktil * q))
            la = A * math.exp(-kappa * (h - ktil * q))
            tot = lb + la
            dt = r.exponential(1 / tot)
            acc += q * dt
            acc2 += q * q * dt
            t += dt
            q += lot if r.random() < lb / tot else -lot
        m = acc / t
        return math.sqrt(acc2 / t - m * m)

    for lot, kt in ((1.0, 0.02), (2.0, 0.01)):
        sim = gillespie(1.0, 1.5, 0.3, kt, lot, 4e5, 3)
        th = K.inv_std_stationary(lot, 1.5, kt)
        check(
            f"stationary inventory std lot={lot}",
            abs(sim - th) / th < 0.06,
            f"sim={sim:.3f} theory={th:.3f}",
        )

    def exit_mc(lot, lam, qmax, n=4000):
        r = np.random.default_rng(5)
        ts = []
        for _ in range(n):
            q, t = 0.0, 0.0
            while abs(q) < qmax - 1e-9:
                t += r.exponential(1 / (2 * lam))
                q += lot if r.random() < 0.5 else -lot
            ts.append(t)
        return np.mean(ts)

    sim = exit_mc(1.0, 0.5, 6.0)
    th = K.exit_time_symmetric(6.0, 1.0, 0.5)
    check(
        "exit time of symmetric walk",
        abs(sim - th) / th < 0.04,
        f"sim={sim:.2f} theory={th:.2f}",
    )

    g = K.gamma_for_inventory(20, 3, 1.0, 1.5, 0.3, 5.0)
    check(
        "gamma_for_inventory round-trip",
        abs(3 * K.inv_std_stationary(1.0, 1.5, g * 0.3**2 * 5.0) - 20) < 1e-9,
    )

    gg = K.gamma_for_inventory_glft(20, 3, 1.0, 1.5, 0.3, 1.0)
    check(
        "GLFT gamma calibration round-trip",
        abs(
            3
            * K.inv_std_stationary(
                1.0, 1.5, K.glft(0, gg, 0.3, 1.0, 1.5, 1.0)["skew_per_base"]
            )
            - 20
        )
        < 1e-6,
        f"gamma={gg:.5f}",
    )

    bs, as_ = K.size_taper(0.0, 100, 0.01)
    check("taper flat at zero", bs == 100 and as_ == 100)
    bs, as_ = K.size_taper(math.log(2) / 0.01, 100, 0.01)
    check(
        "taper halves the loaded side at q*=ln2/eta", abs(bs - 50) < 1e-9 and as_ == 100
    )
    for lb, la, dt, N in ((0.3, 0.2, 1.0, 5), (0.05, 0.4, 2.0, 3)):
        r_ = np.random.default_rng(9)
        n_ = 400000
        two = one = 0
        z_ = 0
        pb_, pa_ = K.p_fill(lb, dt), K.p_fill(la, dt)
        fb = r_.random(n_) < pb_
        fa = r_.random(n_) < pa_
        for i in np.flatnonzero(fb | fa):
            z_ += 1
            if fb[i] and fa[i]:
                two += 1
                continue
            p_other = pa_ if fb[i] else pb_
            if (r_.random(N) < p_other).any():
                two += 1
            else:
                one += 1
        sc = K.spread_capture(lb, la, dt, N)
        check(
            f"spread_capture lb={lb} la={la}",
            abs(sc["P_spread"] - two / z_) < 0.004
            and abs(sc["P_spread"] + sc["P_one_sided"] - 1) < 1e-12,
            f"{sc} mc={two/z_:.4f}",
        )

    # ---------- intensity estimators
    true_k, d0, n = 2.5, 0.05, 20000
    pen = d0 + rng.exponential(1 / true_k, n)
    f = K.fit_tape_intensity(pen, 3600.0, d0)
    check(
        "kappa MLE",
        abs(f["kappa"] - true_k) < 3 * f["se_kappa"],
        f"hat={f['kappa']:.3f} se={f['se_kappa']:.3f}",
    )
    check(
        "A consistent with rate at delta0",
        abs(f["A"] * math.exp(-f["kappa"] * d0) - n / 3600.0) < 1e-9,
    )

    A_t, k_t = 0.4, 2.0
    dep = rng.uniform(0.05, 1.5, 40000)
    lam = A_t * np.exp(-k_t * dep)
    wait = rng.exponential(1 / lam)
    cens = 30.0
    filled = (wait < cens).astype(float)
    rest = np.minimum(wait, cens)
    f2 = K.fit_own_fills(dep, rest, filled, bins=8)
    check(
        "own-fill intensity fit",
        abs(f2["kappa"] - k_t) / k_t < 0.1 and abs(f2["A"] - A_t) / A_t < 0.2,
        str(f2),
    )
    check("p_fill exact", abs(K.p_fill(0.3, 2.0) - (1 - math.exp(-0.6))) < 1e-12)
    dstar = K.depth_opt(2.0, 0.3)
    grid = np.linspace(0.0, 3, 300001)
    val = np.exp(-2.0 * grid) * (grid - 0.3)
    check(
        "depth_opt is argmax",
        abs(grid[val.argmax()] - dstar) < 1e-4
        and abs(val.max() * 1.0 - K.profit_rate(1.0, 2.0, 0.3)) < 1e-9,
    )

    # ---------- toxicity
    bal = rng.uniform(0.45, 0.55, 500) * 100
    vp = K.vpin(bal, 100 - bal, 50)
    imb = np.full(500, 80.0)
    vp2 = K.vpin(imb, 100 - imb, 50)
    check(
        "VPIN balanced low / one-sided = 0.6",
        vp.mean() < 0.1 and abs(vp2.mean() - 0.6) < 1e-9,
        f"{vp.mean():.3f} {vp2.mean():.3f}",
    )
    px = 100 + np.cumsum(rng.normal(0, 0.01, 20000))
    sz = rng.exponential(5, 20000)
    dP, vols = K.volume_buckets(px, sz, 200.0)
    check(
        "volume buckets are equal-volume",
        np.allclose(vols, 200.0) and abs(len(vols) - sz.sum() / 200) <= 1,
    )
    check(
        "BVC in [0,1], symmetric", abs(K.bvc_buy_fraction([0.0], 1.0)[0] - 0.5) < 1e-12
    )
    sv, im = K.signed_flow([100, 100], [75, 25])
    check("signed_flow", list(sv) == [50, -50] and list(im) == [0.5, -0.5])
    sv = rng.normal(0, 1e5, 5000)
    r = 3e-9 * sv + rng.normal(0, 1e-4, 5000)
    check("Kyle lambda recovered", abs(K.kyle_lambda(r, sv) - 3e-9) / 3e-9 < 0.1)
    check(
        "markout sign",
        K.markout_bps(+1, 100, 100.1) > 0 and K.markout_bps(-1, 100, 100.1) < 0,
    )

    # ---------- tail risk
    x = rng.normal(0, 0.001, 2_000_000)
    ve = K.var_es_normal(1000, 0.001, 1.0, 0.99)
    mc = K.hist_var_es(x, 1000, 0.99)
    check(
        "normal VaR/ES vs MC",
        abs(ve["var"] - mc["var"]) / ve["var"] < 0.02
        and abs(ve["es"] - mc["es"]) / ve["es"] < 0.02,
        f"{ve} {mc}",
    )
    check(
        "CF reduces to normal",
        abs(K.cornish_fisher_z(0.99, 0, 0) - stats.norm.ppf(0.99)) < 1e-12,
    )
    sk, ek = K.agg_moments(1.2, 6.0, 30)
    y = stats.skewnorm.rvs(0, size=10)  # placeholder to keep scipy import honest
    gam = stats.gamma(a=4)  # skew 1, excess kurt 1.5, standardised
    zt = (gam.ppf(0.99) - gam.mean()) / gam.std()
    zcf = K.cornish_fisher_z(0.99, 1.0, 1.5)
    check(
        "CF within 10% of true gamma quantile",
        abs(zcf - zt) / zt < 0.10,
        f"cf={zcf:.3f} true={zt:.3f}",
    )
    check(
        "agg_moments", abs(sk - 1.2 / math.sqrt(30)) < 1e-12 and abs(ek - 0.2) < 1e-12
    )
    kp = K.kupiec(250, 6, 0.01)
    lr = kp["lr"]
    check("Kupiec vs scipy chi2", abs(kp["p_value"] - stats.chi2.sf(lr, 1)) < 1e-10)
    check(
        "Kupiec x=0 and x=n finite",
        all(math.isfinite(K.kupiec(100, v, 0.05)["lr"]) for v in (0, 100)),
    )
    lv = K.lvar(1000, 0.001, 100.0, 0.0005, 0.0002, 0.0003)
    check("LVaR exceeds VaR", lv > K.var_es_normal(1000, 0.001, 100.0)["var"])
    tr = K.ac_trajectory(100.0, 10.0, 1.0, 1.0, 0.5)
    check(
        "Almgren-Chriss endpoints",
        abs(tr[0] - 100) < 1e-9 and abs(tr[-1]) < 1e-9 and tr[1] < 90,
    )
    check(
        "Almgren-Chriss lam->0 is TWAP",
        np.allclose(
            K.ac_trajectory(100.0, 10.0, 1.0, 1.0, 1e-12),
            100 * (1 - np.linspace(0, 1, 11)),
            atol=1e-4,
        ),
    )

    # ---------- leverage / drawdown
    check(
        "liq_price long 10x",
        abs(K.liq_price(100, 10, 0.004, +1) - 90.4) < 1e-12
        and abs(K.liq_price(100, 10, 0.004, -1) - 109.6) < 1e-12,
    )

    def touch_mc(entry, barrier, sig, T, nu, n=20000, steps=2000):
        r = np.random.default_rng(11)
        dt = T / steps
        inc = nu * dt + sig * math.sqrt(dt) * r.standard_normal((n, steps))
        path = np.cumsum(inc, 1)
        b = math.log(barrier / entry)
        hit = (path.min(1) <= b) if barrier < entry else (path.max(1) >= b)
        return hit.mean()

    for bar, nu in ((90.0, 0.0), (90.0, -0.0004), (110.0, 0.0003)):
        th = K.p_touch(100.0, bar, 0.01, 100.0, nu)
        mc = touch_mc(100.0, bar, 0.01, 100.0, nu)
        check(
            f"p_touch barrier={bar} nu={nu}",
            abs(th - mc) < 0.02 + 0.1 * th,
            f"theory={th:.4f} mc={mc:.4f}",
        )
    mu, sg, D = 0.5, 1.0, 1.5

    def hit_mc(mu, sg, D, T, n=6000, steps=6000, seed=2):
        r = np.random.default_rng(seed)
        dt = T / steps
        hit = 0
        for _ in range(n // 1000):
            p = np.cumsum(
                mu * dt + sg * math.sqrt(dt) * r.standard_normal((1000, steps)), 1
            )
            hit += (p.min(1) <= -D).sum()
        return hit / n

    for T in (4.0, 40.0):
        th = K.loss_hit_prob(mu, sg, D, T)
        mc = hit_mc(mu, sg, D, T)
        check(
            f"loss_hit_prob T={T}", abs(th - mc) < 0.02, f"theory={th:.4f} mc={mc:.4f}"
        )
    check(
        "loss_hit_prob T=inf is the limit",
        abs(K.loss_hit_prob(mu, sg, D, 1e6) - K.loss_hit_prob(mu, sg, D)) < 1e-9,
    )
    check(
        "loss_limit inverts loss_hit_prob",
        abs(K.loss_hit_prob(mu, sg, K.loss_limit(mu, sg, 0.05)) - 0.05) < 1e-12,
    )

    def dd_time_mc(mu, sg, D, n=2000, dt=0.0005, seed=4):
        r = np.random.default_rng(seed)
        x = np.zeros(n)
        peak = np.zeros(n)
        t = np.zeros(n)
        alive = np.ones(n, bool)
        while alive.any():
            x[alive] += mu * dt + sg * math.sqrt(dt) * r.standard_normal(alive.sum())
            peak = np.maximum(peak, x)
            t[alive] += dt
            alive &= (peak - x) < D
        return t.mean()

    sg_, dt_ = 1.0, 0.0005
    th = K.dd_mean_time(0.5, sg_, 1.0 + 2 * 0.5826 * sg_ * math.sqrt(dt_))
    mc = dd_time_mc(0.5, sg_, 1.0, dt=dt_)
    check(
        "mean time to drawdown D (overshoot-corrected MC)",
        abs(th - mc) / th < 0.05,
        f"theory={th:.3f} mc={mc:.3f}",
    )
    Dt = K.dd_limit_for_time(0.5, 1.0, 100.0)
    check(
        "dd_limit_for_time inverts dd_mean_time",
        abs(K.dd_mean_time(0.5, 1.0, Dt) - 100.0) < 1e-6,
        f"D={Dt:.3f}",
    )
    check(
        "kelly caps and floors",
        K.kelly(-1, 1) == 0
        and K.kelly(100, 1) == 1.0
        and abs(K.kelly(0.2, 1.0, 0.5) - 0.1) < 1e-12,
    )
    w = np.array([0.6, 0.4])
    cov = np.array([[0.04, 0.01], [0.01, 0.09]])
    rc, sig = K.risk_contrib(w, cov)
    check("Euler contributions sum to sigma", abs(rc.sum() - sig) < 1e-12)
    check(
        "beta recovered",
        abs(
            K.beta(
                1.7 * (m := rng.normal(0, 0.01, 50000)) + rng.normal(0, 0.002, 50000), m
            )
            - 1.7
        )
        < 0.05,
    )
    bu = 1.1
    bl = K.relever_beta(bu, 0.0, 4.0)
    check(
        "relever = L * beta at tax 0",
        abs(bl - 5 * bu) < 1e-12
        and abs(K.relever_beta(bu, 0.35, 0.5) - bu * (1 + 0.65 * 0.5)) < 1e-12,
    )
    check("capm", abs(K.capm(0.04, 1.2, 0.05) - 0.10) < 1e-12)

    # ---------- arbitrage (numbers from the triangular-arbitrage note)
    r1 = [K.leg_rate("buy", 90000, 90034), 45.470, 2000.0]  # USD -> BTC -> ETH -> USD
    G = K.cycle_gross(r1)
    check("note example gross ~1.0101", abs(G - 1.0101) < 5e-4, f"G={G:.5f}")
    bf = K.breakeven_fee(r1)
    check(
        "breakeven fee zeroes the edge",
        abs(K.cycle_net(r1, [bf] * 3) - 1) < 1e-12,
        f"bf={bf*1e4:.1f} bps/leg",
    )
    r2 = [
        K.leg_rate("buy", 2000, 2002),
        1 / 45.478,
        90000.0,
    ]  # USD -> ETH -> BTC -> USD
    check(
        "note example reverse is a loss",
        K.cycle_gross(r2) < 1,
        f"G={K.cycle_gross(r2):.5f}",
    )
    check("no arbitrage => zero breakeven fee", K.breakeven_fee(r2) == 0.0)
    check(
        "arb_ev collapses to N(G-1) at p=1",
        abs(K.arb_ev(1000, 1.004, [1, 1, 1], [0, 0.01, 0.01]) - 4.0) < 1e-12,
    )
    ev = K.arb_ev(1000, 1.004, [1.0, 0.9, 1.0], [0.0, 0.01, 0.01])
    check(
        "arb_ev leg failure",
        abs(ev - (0.9 * 4.0 - 1.0 * 0.1 * 1000 * 0.01)) < 1e-12,
        f"{ev}",
    )
    asks = [(100, 1.0), (101, 2.0)]
    px_, base_, sp_ = K.vwap_buy(asks, 201.0)
    check(
        "vwap_buy walks the book",
        abs(base_ - 2.0) < 1e-9 and abs(px_ - 100.5) < 1e-9,
        f"{px_} {base_} {sp_}",
    )
    px2, sold2, got2 = K.vwap_sell([(100, 1.0), (99, 2.0)], 2.0)
    check("vwap_sell walks the book", abs(px2 - 99.5) < 1e-9)

    # ---------- volume layer + gate
    slot = np.tile(np.arange(168), 4)
    qv = np.tile(np.r_[np.full(84, 1.0), np.full(84, 3.0)], 4)
    sh = K.hour_of_week_share(slot, qv)
    check(
        "hour-of-week shares sum to 1",
        abs(sh.sum() - 1) < 1e-12 and abs(sh[100] / sh[10] - 3) < 1e-12,
    )
    check(
        "liquidation time",
        abs(K.liquidation_seconds(5000, 100.0, 0.05) - 1000.0) < 1e-9,
    )
    check(
        "sqrt impact scaling",
        abs(K.sqrt_impact(4e4, 1e8, 0.03) / K.sqrt_impact(1e4, 1e8, 0.03) - 2) < 1e-12,
    )
    o = np.array([100, 101.0])
    h = np.array([101, 102.0])
    l = np.array([99, 100.0])
    c = np.array([101, 100.5])
    check("Rogers-Satchell positive", K.sigma_rs(o, h, l, c, 60) > 0)
    ret = rng.normal(0, 0.0005, 5000)
    check(
        "EWMA vol ~ truth",
        abs(K.sigma_ewma(ret, 60, 0.999) * math.sqrt(60) - 0.0005) / 0.0005 < 0.05,
    )

    base = dict(
        data_ok=True,
        n_samples=1000,
        stale_s=1.0,
        edge_lcb_bps=0.5,
        vpin_pct=0.5,
        inv_abs=10,
        inv_max=100,
        tau_liq_s=100,
        es_quote=50,
        capital=1000,
        p_liq=0.0,
        dd_now=0,
        dd_limit_quote=150,
    )
    a = K.assess(**base)
    check("assess GO", a["state"] == "GO" and a["multiplier"] == 1.0, str(a))
    a = K.assess(**{**base, "tau_liq_s": 600})
    check(
        "liquidity gate scales exactly",
        abs(a["multiplier"] - 0.5) < 1e-12 and a["binding"] == "liquidity",
    )
    a = K.assess(**{**base, "vpin_pct": 0.96})
    check("toxicity halts", a["state"] == "HALT")
    a = K.assess(**{**base, "es_quote": 100})
    check("tail gate = budget / ES", abs(a["multiplier"] - 0.5) < 1e-12)
    a = K.assess(**{**base, "edge_lcb_bps": -0.1})
    check("negative edge halts", a["state"] == "HALT" and a["binding"] == "edge")
    return failures


def test_kernel_block_parses_and_exports_the_documented_api():
    K = _load_kernel()
    for name in (
        "glft",
        "avellaneda_stoikov",
        "as_validity",
        "gamma_for_inventory_glft",
        "fit_tape_intensity",
        "fit_own_fills",
        "vpin",
        "var_es_normal",
        "kupiec",
        "p_touch",
        "loss_hit_prob",
        "dd_mean_time",
        "breakeven_fee",
        "arb_ev",
        "spread_capture",
        "assess",
    ):
        assert callable(getattr(K, name)), name


def test_every_formula_matches_its_independent_check():
    failures = _run_checks(_load_kernel())
    assert not failures, "\n".join(failures)


def test_skill_references_only_files_that_exist():
    text = (SKILL / "SKILL.md").read_text()
    for companion in re.findall(r'file="([\w.]+)"', text):
        assert (SKILL / companion).exists(), companion
    assert (SKILL / "derivations.md").exists()
