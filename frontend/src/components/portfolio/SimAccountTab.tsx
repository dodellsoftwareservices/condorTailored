import { FlaskConical } from "lucide-react";

import { SIM_TAB_BLURB, SIM_TAB_LABEL, useSimAccount, type SimVariant } from "./simAccountData";

const num = (v: string | null | undefined) => {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
};
const usd = (n: number, digits = 2) =>
  n.toLocaleString("en-US", { style: "currency", currency: "USD", minimumFractionDigits: digits, maximumFractionDigits: digits });
const signed = (n: number) => `${n > 0 ? "+" : n < 0 ? "-" : ""}${usd(Math.abs(n))}`;
const tone = (n: number) => (n > 0 ? "var(--color-green)" : n < 0 ? "var(--color-red)" : undefined);
const qty = (n: number) => n.toLocaleString("en-US", { maximumFractionDigits: 8 });

function Stat({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div className="min-w-0">
      <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)]">{label}</div>
      <div className="text-base font-semibold tabular-nums mt-0.5" style={{ color }}>{value}</div>
    </div>
  );
}

const th = "px-4 py-3 text-xs font-medium uppercase tracking-wider text-[var(--color-text-muted)]";

/** What the simulated account holds, in the same shape as the Assets table. */
export function SimAccountTab({ variant }: { variant: SimVariant }) {
  const mm = variant === "mm";
  const { pf, costs, maker } = useSimAccount(variant);
  if (!pf) return null;

  const equity = num(pf.equity);
  const cash = num(pf.cash);
  const startCash = num(pf.start_cash);
  // The API rounds total_return_pct to two decimals, hiding a $4 loss on $100k.
  const ret = startCash > 0 ? ((equity - startCash) / startCash) * 100 : num(pf.total_return_pct);
  const retDigits = ret !== 0 && Math.abs(ret) < 0.01 ? 4 : 2;
  const net = costs ? num(costs.net_pnl_after_all_costs) : 0;
  const fees = costs ? num(costs.costs.exchange_fees) : 0;
  const fills = costs ? costs.fills.maker + costs.fills.taker : 0;
  const share = (v: number) => (equity > 0 ? (v / equity) * 100 : 0);
  const rows = pf.positions.filter((p) => num(p.qty) !== 0);
  const executed = costs?.recent_executed_orders ?? [];
  const pairs = Object.entries(maker?.pairs ?? {});

  return (
    <div className="space-y-4" data-testid="sim-account-tab" data-variant={variant}>
      <div className="flex flex-wrap items-center gap-2">
        <FlaskConical className="h-4 w-4 text-[var(--color-primary)]" />
        <h2 className="text-sm font-semibold">{SIM_TAB_LABEL[variant]}</h2>
        <span className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-primary)]">
          Simulated · no real funds
        </span>
        <span className="text-xs text-[var(--color-text-muted)]">{SIM_TAB_BLURB[variant]}</span>
        <span className="ml-auto text-xs text-[var(--color-text-muted)]">Live Kraken prices · simulated fills and fees</span>
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-8 gap-x-6 gap-y-3 rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
        <Stat label="Equity" value={usd(equity)} />
        <Stat label="Return" value={`${ret >= 0 ? "+" : ""}${ret.toFixed(retDigits)}%`} color={tone(ret)} />
        <Stat label="Cash" value={usd(cash)} />
        <Stat label="Exposure" value={usd(num(pf.gross_exposure))} />
        <Stat label="Net P&L after costs" value={signed(net)} color={tone(net)} />
        <Stat label="Fees paid" value={usd(fees)} />
        <Stat label="Fills" value={String(fills)} />
        <Stat label="Open orders" value={String(pf.open_orders)} />
        {variant === "fut" && <Stat label="Leverage" value={`${num(pf.leverage).toFixed(2)}x`} />}
        {variant === "fut" && (
          <Stat label="Funding paid" value={signed(num(pf.funding_paid))} color={num(pf.funding_paid) > 0 ? "var(--color-red)" : num(pf.funding_paid) < 0 ? "var(--color-green)" : undefined} />
        )}
        {variant === "fut" && <Stat label="Liquidations" value={String(pf.liquidations ?? 0)} color={pf.liquidations ? "var(--color-red)" : undefined} />}
      </div>

      {mm && maker && (
        <div className="text-xs text-[var(--color-text-muted)] leading-relaxed" data-testid="sim-mm-status">
          <span style={{ color: maker.halted ? "var(--color-red)" : maker.running ? "var(--color-green)" : undefined }}>
            {maker.halted ? `Halted: ${maker.halted}` : maker.running ? "Quoting" : "Stopped"}
          </span>
          {maker.configured && (
            <>
              {" · "}{maker.cycles ?? 0} cycles · {maker.quotes_placed ?? 0} quotes placed · {maker.quotes_cancelled ?? 0} cancelled ·{" "}
              {maker.maker_fills ?? 0} fills · volume {usd(num(maker.volume), 0)}
              {maker.last_error ? ` · last error: ${maker.last_error}` : ""}
            </>
          )}
        </div>
      )}

      <div className="portfolio-table-scroll" role="region" aria-label={`${SIM_TAB_LABEL[variant]} holdings, scroll horizontally for all columns`} tabIndex={0}>
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-[var(--color-border)] bg-[var(--color-surface)]">
              <th className={`${th} text-left`}>Asset</th>
              <th className={`${th} text-right`}>Amount</th>
              <th className={`${th} text-right`}>Avg cost</th>
              <th className={`${th} text-right`}>Price</th>
              <th className={`${th} text-right`}>Value</th>
              <th className={`${th} text-right`}>Unrealized P&L</th>
              <th className={`${th} text-right`}>Allocation</th>
            </tr>
          </thead>
          <tbody>
            <tr className="border-b border-[var(--color-border)]" data-testid="sim-cash-row">
              <td className="px-4 py-2 font-medium">{variant === "fut" ? "USD collateral" : "USD"}</td>
              <td className="px-4 py-2 text-right tabular-nums">{usd(cash)}</td>
              <td className="px-4 py-2 text-right text-[var(--color-text-muted)]">—</td>
              <td className="px-4 py-2 text-right tabular-nums text-[var(--color-text-muted)]">$1.00</td>
              <td className="px-4 py-2 text-right tabular-nums font-medium">{usd(cash)}</td>
              <td className="px-4 py-2 text-right text-[var(--color-text-muted)]">—</td>
              <td className="px-4 py-2 text-right tabular-nums text-[var(--color-text-muted)]">{share(cash).toFixed(1)}%</td>
            </tr>
            {rows.map((p) => {
              const value = num(p.value);
              const pnl = num(p.unrealized_pnl);
              return (
                <tr key={p.asset} className="border-b border-[var(--color-border)]" data-testid="sim-position-row">
                  <td className="px-4 py-2 font-medium">
                    {p.asset}
                    {p.side && <span className="ml-2 text-[10px] uppercase tracking-wider text-[var(--color-text-muted)]">{p.side}</span>}
                  </td>
                  <td className="px-4 py-2 text-right tabular-nums">{qty(num(p.qty))}</td>
                  <td className="px-4 py-2 text-right tabular-nums text-[var(--color-text-muted)]">{p.avg_cost ? usd(num(p.avg_cost)) : "—"}</td>
                  <td className="px-4 py-2 text-right tabular-nums text-[var(--color-text-muted)]">{p.price ? usd(num(p.price)) : "—"}</td>
                  <td className="px-4 py-2 text-right tabular-nums font-medium">{p.value ? usd(value) : "—"}</td>
                  <td className="px-4 py-2 text-right tabular-nums" style={{ color: tone(pnl) }}>{p.unrealized_pnl ? signed(pnl) : "—"}</td>
                  <td className="px-4 py-2 text-right tabular-nums text-[var(--color-text-muted)]">{share(value).toFixed(1)}%</td>
                </tr>
              );
            })}
            {rows.length === 0 && (
              <tr>
                <td colSpan={7} className="px-4 py-6 text-center text-xs text-[var(--color-text-muted)]">
                  {mm
                    ? "No inventory yet: the maker only holds a coin once one of its resting bids fills."
                    : fills > 0
                      ? "Flat now: earlier buys were sold. Their fees and trading loss remain in account equity."
                      : "No coin positions: the agent is flat and holding cash."}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {executed.length > 0 && (
        <div>
          <div className="mb-2 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
            <h3 className="text-sm font-semibold">Executed orders</h3>
            <p className="text-xs text-[var(--color-text-muted)]">Partial fills are grouped by order. Sell P&L includes entry and exit fees.</p>
          </div>
          <div className="portfolio-table-scroll" role="region" aria-label={`${SIM_TAB_LABEL[variant]} executed orders`} tabIndex={0}>
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-[var(--color-border)] bg-[var(--color-surface)]">
                  <th className={`${th} text-left`}>Filled</th>
                  <th className={`${th} text-left`}>Pair</th>
                  <th className={`${th} text-left`}>Side</th>
                  <th className={`${th} text-right`}>Notional</th>
                  <th className={`${th} text-right`}>Fees</th>
                  <th className={`${th} text-right`}>Closed P&L</th>
                  <th className={`${th} text-left`}>Reason</th>
                </tr>
              </thead>
              <tbody>
                {executed.map((order) => (
                  <tr key={order.order_id} className="border-b border-[var(--color-border)]" data-testid="sim-executed-order">
                    <td className="px-4 py-2 whitespace-nowrap text-[var(--color-text-muted)]">{new Date(order.last_fill_ts * 1000).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}</td>
                    <td className="px-4 py-2 font-medium">{order.pair}</td>
                    <td className="px-4 py-2 uppercase">{order.side} <span className="normal-case text-xs text-[var(--color-text-muted)]">· {order.fill_count} {order.fill_count === 1 ? "fill" : "fills"}</span></td>
                    <td className="px-4 py-2 text-right tabular-nums">{usd(num(order.notional))}</td>
                    <td className="px-4 py-2 text-right tabular-nums">{usd(num(order.fee))}</td>
                    <td className="px-4 py-2 text-right tabular-nums" style={{ color: tone(num(order.realized_pnl)) }}>{order.side === "sell" ? signed(num(order.realized_pnl)) : "—"}</td>
                    <td className="px-4 py-2">{order.calibration ? <span className="text-[var(--color-red)]">Calibration</span> : "Strategy"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {mm && pairs.length > 0 && (
        <div>
          <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] mb-2">Resting quotes</div>
          <div className="portfolio-table-scroll" role="region" aria-label="Market maker resting quotes" tabIndex={0}>
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-[var(--color-border)] bg-[var(--color-surface)]">
                  <th className={`${th} text-left`}>Pair</th>
                  <th className={`${th} text-right`}>Bid</th>
                  <th className={`${th} text-right`}>Mid</th>
                  <th className={`${th} text-right`}>Ask</th>
                  <th className={`${th} text-right`}>Half-spread</th>
                  <th className={`${th} text-right`}>Maker fee</th>
                </tr>
              </thead>
              <tbody>
                {pairs.map(([pair, q]) => (
                  <tr key={pair} className="border-b border-[var(--color-border)]" data-testid="sim-quote-row">
                    <td className="px-4 py-2 font-medium">{pair}</td>
                    <td className="px-4 py-2 text-right tabular-nums">{q.bid ? usd(num(q.bid)) : "—"}</td>
                    <td className="px-4 py-2 text-right tabular-nums text-[var(--color-text-muted)]">{usd(num(q.mid))}</td>
                    <td className="px-4 py-2 text-right tabular-nums">{q.ask ? usd(num(q.ask)) : "—"}</td>
                    <td className="px-4 py-2 text-right tabular-nums">±{num(q.half_spread_bps).toFixed(0)} bps</td>
                    <td className="px-4 py-2 text-right tabular-nums text-[var(--color-text-muted)]">{num(q.maker_fee_bps).toFixed(0)} bps/side</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
