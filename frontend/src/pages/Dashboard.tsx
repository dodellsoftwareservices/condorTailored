import { useQuery } from "@tanstack/react-query";
import { Activity, ArrowDownRight, ArrowUpRight, ChevronRight, CircleHelp, Clock3, ExternalLink, LayoutDashboard, RefreshCw, ShieldCheck, Wallet } from "lucide-react";
import { useId, useState, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Area, AreaChart, CartesianGrid, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { useServer } from "@/hooks/useServer";
import { api, type DashboardAccount } from "@/lib/api";
import { useAuth } from "@/lib/auth";

import "./dashboard.css";

const RANGES = ["1D", "1W", "1M", "3M"];
const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 2 });
const compact = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });
const money = (value: number | null | undefined) => value == null ? "—" : usd.format(value);
const percent = (value: number | null | undefined) => value == null ? "—" : `${value > 0 ? "+" : ""}${value.toFixed(value !== 0 && Math.abs(value) < 0.01 ? 4 : 2)}%`;
const signed = (value: number | null | undefined) => value == null ? "—" : `${value > 0 ? "+" : ""}${usd.format(value)}`;
const tone = (value: number | null | undefined) => value == null || value === 0 ? "" : value > 0 ? "is-positive" : "is-negative";

function Panel({ title, caption, action, children, className = "" }: { title: string; caption?: string; action?: ReactNode; children: ReactNode; className?: string }) {
  return <section className={`dashboard-panel ${className}`}>
    <header className="dashboard-panel-head"><div><h2>{title}</h2>{caption && <p>{caption}</p>}</div>{action}</header>
    {children}
  </section>;
}

function Metric({ label, value, detail, hint, trend, loading }: { label: string; value: string; detail: string; hint: string; trend?: number | null; loading: boolean }) {
  return <section className={`dashboard-metric ${tone(trend)}`} aria-label={label}>
    <div className="dashboard-metric-label">{label}<CircleHelp size={12} tabIndex={0} aria-label={`${label}: ${hint}`}><title>{hint}</title></CircleHelp><span className="sr-only">{hint}</span></div>
    <div className={`dashboard-metric-value ${loading ? "dashboard-skeleton" : ""}`} aria-busy={loading}>{loading ? "\u00a0" : value}</div>
    <div className="dashboard-metric-detail">{trend != null && trend !== 0 && (trend > 0 ? <ArrowUpRight size={12} /> : <ArrowDownRight size={12} />)}{detail}</div>
  </section>;
}

function EquityChart({ account, range, loading }: { account?: DashboardAccount; range: string; loading: boolean }) {
  const [basis, setBasis] = useState<"equity" | "pnl">("equity");
  const gradient = useId().replace(/:/g, "");
  const selectedBasis = basis === "pnl" && account?.starting_capital == null ? "equity" : basis;
  const points = (account?.points ?? []).map((point) => ({ ...point, value: selectedBasis === "equity" ? point.equity : point.equity - (account?.starting_capital ?? 0) }));
  const latest = points.at(-1)?.value;
  const values = points.map((point) => point.value);
  const span = points.length > 1 ? points[points.length - 1].timestamp - points[0].timestamp : 0;
  const preciseAxis = values.length > 0 && Math.max(...values) - Math.min(...values) < 1000;
  const observed = points.length > 1 ? `${new Date(points[0].timestamp * 1000).toLocaleDateString("en-US", { month: "short", day: "numeric" })}–${new Date(points[points.length - 1].timestamp * 1000).toLocaleDateString("en-US", { month: "short", day: "numeric" })}` : "History collecting";
  return <Panel title={selectedBasis === "equity" ? "Portfolio equity" : "Cumulative PnL"} caption={`${account?.id === "all" && account.label === "All paper accounts" ? "Tracked paper accounts" : account?.label || "Your accounts"} · ${observed} observed · ${range} selected`}
    action={<div className="dashboard-segments" aria-label="Chart measure">{(["equity", "pnl"] as const).map((mode) => <button key={mode} aria-pressed={selectedBasis === mode} disabled={mode === "pnl" && account?.starting_capital == null} onClick={() => setBasis(mode)}>{mode === "equity" ? "Equity" : "PnL"}</button>)}</div>}
    className="dashboard-equity">
    <div className="dashboard-chart-reading">{loading ? "—" : selectedBasis === "equity" ? money(latest) : signed(latest)}<span>{points.length} recorded observations</span></div>
    {loading ? <div className="dashboard-chart-empty" role="status"><RefreshCw className="animate-spin" size={20} />Loading account history…</div> : points.length < 2 ? <div className="dashboard-chart-empty"><Activity size={28} /><strong>History starts here</strong><span>Equity observations appear as accounts report. Returns and risk metrics need recorded history.</span></div> : <div className="dashboard-chart" role="img" aria-label={`${selectedBasis === "equity" ? "Equity" : "PnL"} chart for ${range}, ${points.length} observations`}>
      <ResponsiveContainer width="100%" height={278} minWidth={0}>
        <AreaChart data={points} margin={{ top: 12, right: 12, left: 0, bottom: 4 }}>
          <defs><linearGradient id={gradient} x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="var(--color-primary)" stopOpacity={0.24} /><stop offset="100%" stopColor="var(--color-primary)" stopOpacity={0.015} /></linearGradient></defs>
          <CartesianGrid stroke="var(--color-border)" strokeDasharray="3 5" vertical={false} />
          <XAxis dataKey="timestamp" type="number" domain={["dataMin", "dataMax"]} tickLine={false} axisLine={false} minTickGap={45} tick={{ fill: "var(--color-text-muted)", fontSize: 10 }} tickFormatter={(stamp) => span < 2 * 86400 ? new Date(stamp * 1000).toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", ...(span < 300 ? { second: "2-digit" } : {}) }) : new Date(stamp * 1000).toLocaleDateString("en-US", { month: "short", day: "numeric" })} />
          <YAxis width={preciseAxis ? 92 : 62} domain={["auto", "auto"]} tickLine={false} axisLine={false} tick={{ fill: "var(--color-text-muted)", fontSize: 10 }} tickFormatter={(value) => preciseAxis ? money(value) : `$${compact.format(value)}`} />
          <Tooltip contentStyle={{ background: "var(--color-surface)", border: "1px solid var(--color-border)", borderRadius: 4, fontSize: 12 }} labelFormatter={(stamp) => new Date(Number(stamp) * 1000).toLocaleString()} formatter={(value) => [money(Number(value)), selectedBasis === "equity" ? "Equity" : "PnL"]} />
          {selectedBasis === "equity" && account?.starting_capital != null && <ReferenceLine y={account.starting_capital} ifOverflow="extendDomain" stroke="var(--color-text-muted)" strokeDasharray="4 4" strokeOpacity={0.6} label={{ value: "Starting capital", position: "insideTopRight", fill: "var(--color-text-muted)", fontSize: 10 }} />}
          {selectedBasis === "pnl" && <ReferenceLine y={0} stroke="var(--color-text-muted)" strokeDasharray="4 4" />}
          <Area dataKey="value" type="linear" stroke="var(--color-primary)" strokeWidth={2} fill={`url(#${gradient})`} isAnimationActive={false} dot={points.length < 4 ? { r: 3 } : false} />
        </AreaChart>
      </ResponsiveContainer>
    </div>}
    <footer className="dashboard-chart-foot"><span><i />{selectedBasis === "equity" ? "Marked account equity" : "Equity less starting capital"}</span><span>Observed history · no synthetic data</span></footer>
  </Panel>;
}

export function Dashboard() {
  const { user } = useAuth();
  const { server } = useServer();
  const [params, setParams] = useSearchParams();
  const isAdmin = user?.role === "admin";
  const mode = params.get("mode") === "live" || !isAdmin ? "live" : "paper";
  const range = RANGES.includes(params.get("range") || "") ? params.get("range")! : "1M";
  const accountId = params.get("account") || "all";
  const [cadence, setCadence] = useState(30_000);
  const update = (values: Record<string, string>) => setParams((previous) => { const next = new URLSearchParams(previous); Object.entries(values).forEach(([key, value]) => next.set(key, value)); return next; }, { replace: true });

  const paper = useQuery({ queryKey: ["dashboard-paper", range], queryFn: ({ signal }) => api.getPaperDashboard(range, signal), enabled: mode === "paper" && isAdmin, refetchInterval: cadence || false, retry: false, staleTime: 20_000 });
  const live = useQuery({ queryKey: ["portfolio", server], queryFn: () => api.getPortfolio(server!), enabled: mode === "live" && !!server, refetchInterval: cadence || false, retry: false });
  const history = useQuery({ queryKey: ["portfolio-history", server, range], queryFn: () => api.getPortfolioHistory(server!, range), enabled: mode === "live" && !!server, refetchInterval: cadence || false, retry: false });
  const accounts = paper.data?.accounts ?? [];
  const liveAccount: DashboardAccount | undefined = live.data ? { id: "live", label: server || "Live exchanges", status: "online", starting_capital: null, equity: live.data.total_usd, cash: null, deployed: null, pnl: null, roi_pct: null, volume: null, fees: null, batting_average: null, closed_trades: null, wins: null, losses: null, breakeven: null, profit_factor: null, sharpe: null, daily_observations: 0, max_drawdown_pct: null, outcomes_complete: false, notes: [], points: (history.data?.points ?? []).map((p) => ({ timestamp: p.timestamp, equity: p.total_usd })) } : undefined;
  const account = mode === "paper" ? accounts.find((a) => a.id === accountId) ?? paper.data?.summary : liveAccount;
  const loading = mode === "paper" ? paper.isLoading : live.isLoading;
  const fetching = mode === "paper" ? paper.isFetching : live.isFetching || history.isFetching;
  const error = mode === "paper" ? paper.error : live.error || history.error;
  const updated = mode === "paper" ? (paper.data?.as_of ?? 0) * 1000 : live.dataUpdatedAt;
  const refresh = () => { if (mode === "paper") void paper.refetch(); else { void live.refetch(); void history.refetch(); } };
  const partial = mode === "paper" && accounts.some((a) => a.status !== "online");
  const batting = account?.batting_average;
  const exposure = account?.equity != null && account?.deployed != null && account.equity > 0 ? account.deployed / account.equity * 100 : null;

  return <div className="dashboard-hub">
    <div className="dashboard-breadcrumb"><LayoutDashboard size={13} /> WORKSPACE <ChevronRight size={11} /><span>OVERVIEW</span></div>
    <header className="dashboard-header"><div><div className="dashboard-title-line"><h1>Portfolio overview</h1><span className={`dashboard-mode-badge ${mode === "paper" ? "is-paper" : ""}`}><ShieldCheck size={12} />{mode === "paper" ? "PAPER TRADING" : "LIVE BALANCES"}</span></div><p>Your capital, account health, and trading results in one place.</p></div>
      <div className="dashboard-header-links"><Link to="/agents">Open agents <ExternalLink size={12} /></Link><Link to="/settings?tab=keys">Manage connections <ExternalLink size={12} /></Link></div>
    </header>
    <div className="dashboard-toolbar"><div className="dashboard-filters"><div className="dashboard-segments" aria-label="Account environment"><button aria-pressed={mode === "paper"} disabled={!isAdmin} onClick={() => update({ mode: "paper", account: "all" })}>Paper accounts</button><button aria-pressed={mode === "live"} onClick={() => update({ mode: "live", account: "all" })}>Live exchanges</button></div>{mode === "paper" && <select aria-label="Account" value={accounts.some((a) => a.id === accountId) ? accountId : "all"} onChange={(e) => update({ account: e.target.value })}><option value="all">Tracked paper accounts</option>{accounts.map((a) => <option key={a.id} value={a.id}>{a.label}</option>)}</select>}</div>
      <div className="dashboard-refresh"><div className="dashboard-segments" aria-label="History range">{RANGES.map((period) => <button key={period} aria-pressed={range === period} onClick={() => update({ range: period })}>{period}</button>)}</div><select aria-label="Auto refresh" value={cadence} onChange={(e) => setCadence(Number(e.target.value))}><option value={15000}>15s refresh</option><option value={30000}>30s refresh</option><option value={60000}>1m refresh</option><option value={0}>Refresh off</option></select><button className="dashboard-refresh-button" onClick={refresh} disabled={fetching || (mode === "live" && !server)} aria-label="Refresh dashboard"><RefreshCw size={14} className={fetching ? "animate-spin" : ""} /></button></div>
    </div>
    {error && <div className="dashboard-notice is-error" role="alert">Could not refresh the dashboard. {error.message}<button onClick={refresh}>Retry</button></div>}
    {partial && <div className="dashboard-notice" role="status">Some accounts are unavailable. Combined totals stay unavailable until every account reports; select an account to inspect its results.</div>}
    {mode === "live" && <div className="dashboard-notice">{server ? `Connected exchange balances on ${server}. Paper funds are shown separately. Starting capital and execution-level performance are not yet available for live accounts.` : "Select a server in the toolbar to see connected live exchange balances. Paper accounts work without a server."}</div>}
    <div className="dashboard-section-heading"><div><span>01 / CAPITAL</span><h2>At a glance</h2></div><p>{mode === "paper" ? "Starting capital from the paper accounts shown here." : "Balances from the selected live server."}</p></div>
    <div className="dashboard-metrics dashboard-metrics-primary">
      <Metric label="Total invested" value={money(account?.starting_capital)} detail="Initial account funding · USD" hint="Starting account equity for the accounts shown here. This is not buying power or the per-order cap." loading={loading} />
      <Metric label="Portfolio value" value={money(account?.equity)} detail={account?.cash != null ? `${money(account.cash)} in cash` : "Current marked account equity"} hint="Cash plus marked holdings, not leveraged buying power. Paper and live balances are never added together." loading={loading} />
      <Metric label="Net PnL" value={signed(account?.pnl)} detail="Since initial funding · after execution costs" hint="Current account equity minus starting capital. Simulator operating/LLM costs are tracked separately from account equity." trend={account?.pnl} loading={loading} />
      <Metric label="Return on investment" value={percent(account?.roi_pct)} detail="Since initial funding" hint="Net PnL divided by starting capital. Not annualized; resets or added funding change the reference basis." trend={account?.roi_pct} loading={loading} />
    </div>
    <div className="dashboard-main-grid"><EquityChart key={`${mode}-${account?.id ?? "all"}`} account={account} range={range} loading={loading} />
      <Panel title="Capital allocation" caption="Current exposure and available cash" className="dashboard-allocation">
        <div className="dashboard-allocation-amount">{money(account?.deployed)}<span>Capital deployed</span></div>
        <div className="dashboard-exposure-label"><span>Gross exposure</span><strong>{exposure == null ? "—" : `${exposure.toFixed(1)}%`}</strong></div>
        <div className="dashboard-exposure-track" role="img" aria-label={exposure == null ? "Exposure unavailable" : `${exposure.toFixed(1)}% gross exposure`}><span style={{ width: `${Math.max(0, Math.min(100, exposure ?? 0))}%` }} /></div>
        <div className="dashboard-allocation-row"><span>Cash balance</span><strong>{money(account?.cash)}</strong></div><div className="dashboard-allocation-row"><span>Exchange fees</span><strong>{money(account?.fees)}</strong></div>
        <p>Order caps limit each order; they do not reduce the account’s invested capital.</p>
      </Panel>
    </div>
    <div className="dashboard-section-heading"><div><span>02 / PERFORMANCE</span><h2>Trading quality &amp; risk</h2></div><p>Statistics use observed history in the selected range.</p></div>
    <div className="dashboard-metrics dashboard-metrics-secondary">
      <Metric label="Batting average" value={batting == null ? "—" : `${batting.toFixed(1)}%`} detail={account?.closed_trades != null ? `${account.wins ?? 0} winning / ${account.closed_trades} closes · ${range}` : "Complete closing history required"} hint="Profitable closed sell orders divided by all matched closed sell orders. Breakeven closes count in the denominator. Partial fills of one order count once." loading={loading} />
      <Metric label="Sharpe ratio" value={account?.sharpe == null ? "—" : account.sharpe.toFixed(2)} detail={`Daily · RF 0% · ${account?.daily_observations ?? 0} observations`} hint="Mean daily return divided by sample standard deviation, with 0% risk-free return. Not annualized. Requires at least 7 daily returns and nonzero variance." trend={account?.sharpe} loading={loading} />
      <Metric label="Max drawdown" value={account?.max_drawdown_pct == null ? "—" : `${account.max_drawdown_pct > 0 ? "−" : ""}${account.max_drawdown_pct.toFixed(2)}%`} detail={`Largest observed peak-to-trough · ${range}`} hint="Maximum drawdown of recorded account equity in the selected history range. Unobserved intraday peaks are not reconstructed." loading={loading} />
      <Metric label="Trading volume" value={money(account?.volume)} detail="Executed notional · available lifetime history" hint="Sum of buy and sell fill notionals; opening and closing legs both count. Incomplete history is shown as unavailable." loading={loading} />
    </div>
    <div className="dashboard-bottom-grid">
      <Panel title={mode === "paper" ? "Account performance" : "Connected exchanges"} caption={mode === "paper" ? "Separate accounts. One consistent starting-capital basis." : "Current balances from the selected server."} action={<Wallet size={15} className="dashboard-muted" />}>
        <div className="dashboard-table-scroll"><table className="dashboard-accounts"><thead><tr><th>Account</th><th>Invested</th><th>Equity</th><th>Net PnL</th><th>ROI</th><th>Order cap</th></tr></thead><tbody>
          {mode === "paper" ? accounts.map((a, index) => <tr key={a.id} className={accountId === a.id ? "is-selected" : ""}><td><button onClick={() => update({ account: a.id })}><span className={`dashboard-account-mark mark-${index}`}>{a.id === "alpaca" ? "A" : "K"}</span><span><strong>{a.label}</strong><small><i className={a.status === "online" ? "online" : "offline"} />{a.status === "online" ? "Connected · simulated funds" : "Unavailable"}</small></span></button></td><td>{money(a.starting_capital)}</td><td>{money(a.equity)}</td><td className={tone(a.pnl)}>{signed(a.pnl)}</td><td className={tone(a.roi_pct)}>{percent(a.roi_pct)}</td><td><span className="dashboard-limit">{money(a.order_cap)}</span></td></tr>) : (live.data?.connectors ?? []).map((c) => <tr key={c.connector}><td><strong>{c.connector}</strong><small>{c.balances.length} assets</small></td><td>—</td><td>{money(c.total_usd)}</td><td>—</td><td>—</td><td>—</td></tr>)}
          {!loading && (mode === "paper" ? accounts.length === 0 : !live.data?.connectors.length) && <tr><td colSpan={6} className="dashboard-table-empty">{mode === "paper" ? "Account data will appear when the dashboard connects." : "No exchange balances available."}</td></tr>}
        </tbody></table></div>
        {mode === "paper" && accounts.flatMap((a) => a.notes.map((note) => <p className="dashboard-account-note" key={`${a.id}-${note}`}><strong>{a.label}:</strong> {note}</p>))}
      </Panel>
      <Panel title="Trading edge" caption={`Settled outcomes · ${range}`} className="dashboard-edge">
        {batting == null || account?.closed_trades === 0 ? <div className="dashboard-edge-empty"><div className="dashboard-edge-empty-icon"><Activity size={22} /></div><strong>{account?.closed_trades === 0 ? "No closed trades yet" : "Trade results unavailable"}</strong><span>{account?.closed_trades === 0 ? "Win rate and profit factor will appear when positions close." : "Complete closing history is needed to calculate win rate and profit factor."}</span></div> : <><div className="dashboard-edge-score"><strong>{batting.toFixed(1)}%</strong><span>winning closes</span></div><div className="dashboard-edge-bar" role="img" aria-label={`${batting.toFixed(1)} percent winning closes`}><span style={{ width: `${Math.max(0, Math.min(100, batting))}%` }} /></div></>}
        {batting != null && (account?.closed_trades ?? 0) > 0 && <><div className="dashboard-outcomes"><div><i className="is-win" /><span>Winning closes</span><strong>{account?.wins ?? "—"}</strong></div><div><i className="is-loss" /><span>Losing closes</span><strong>{account?.losses ?? "—"}</strong></div><div><i /><span>Breakeven</span><strong>{account?.breakeven ?? "—"}</strong></div></div>
          <div className="dashboard-edge-foot"><span>Profit factor <CircleHelp size={12}><title>Gross winning close PnL divided by gross losing close PnL; unavailable when there are no losing closes or the history is incomplete.</title></CircleHelp></span><strong>{account?.profit_factor == null ? "—" : `${account.profit_factor.toFixed(2)}×`}</strong></div></>}
      </Panel>
    </div>
    <footer className="dashboard-footer"><span><i className={error || partial ? "is-warning" : ""} />{error ? "Refresh failed" : fetching ? "Refreshing observations…" : "Read-only account monitoring"}</span><span><Clock3 size={11} />{updated ? `Updated ${new Date(updated).toLocaleTimeString()}` : "Waiting for account data"} · All values USD</span></footer>
  </div>;
}
