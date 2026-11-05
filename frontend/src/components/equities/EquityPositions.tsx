import { useQuery } from "@tanstack/react-query";

import { api, errorStatus, type EquityPosition } from "@/lib/api";

const usd = (n: number) =>
  n.toLocaleString(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 2 });

const signedUsd = (n: number) => `${n > 0 ? "+" : n < 0 ? "−" : ""}${usd(Math.abs(n))}`;

const signedPct = (n: number) => `${n > 0 ? "+" : n < 0 ? "−" : ""}${Math.abs(n * 100).toFixed(2)}%`;

const tone = (n: number) =>
  n > 0
    ? "text-[var(--color-green)]"
    : n < 0
      ? "text-[var(--color-red)]"
      : "text-[var(--color-text-muted)]";

/** Cost basis is market value minus unrealized P&L: the account API reports no cost field. */
function costOf(p: EquityPosition): number {
  return p.market_value - p.unrealized_pl;
}

/**
 * Open positions in the shared Alpaca paper account.
 *
 * These orders go straight to Alpaca rather than through a Hummingbot bot, so
 * the Bots page — which lists bots, controllers and executors — never shows
 * them. This is where equity positions live. Admin-only on the backend, like
 * the account summary above it.
 */
export function EquityPositions({ onSelect }: { onSelect: (symbol: string) => void }) {
  const positionsQuery = useQuery({
    queryKey: ["equity-positions"],
    queryFn: api.getEquityPositions,
    retry: (count, err) => errorStatus(err) !== 403 && count < 2,
    refetchInterval: 30_000,
  });

  // The account summary already explains a 403; say nothing twice.
  if (errorStatus(positionsQuery.error) === 403) return null;

  if (positionsQuery.isError) {
    return (
      <p className="text-sm text-[var(--color-text-muted)]">
        Could not load positions. Retrying shortly.
      </p>
    );
  }

  const positions = [...(positionsQuery.data ?? [])].sort((a, b) => b.market_value - a.market_value);

  if (positionsQuery.isLoading) {
    return <p className="text-sm text-[var(--color-text-muted)]">Loading positions…</p>;
  }

  if (positions.length === 0) {
    return (
      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] px-4 py-3 text-sm text-[var(--color-text-muted)]">
        No open positions.
      </div>
    );
  }

  const totalCost = positions.reduce((s, p) => s + costOf(p), 0);
  const totalValue = positions.reduce((s, p) => s + p.market_value, 0);
  const totalPl = positions.reduce((s, p) => s + p.unrealized_pl, 0);

  return (
    <div className="overflow-hidden rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)]">
      <div className="px-4 py-2.5 text-[10px] uppercase tracking-wider text-[var(--color-text-muted)]">
        Open positions ({positions.length})
      </div>
      <table className="w-full text-sm">
        <thead>
          <tr className="border-y border-[var(--color-border)] text-[10px] uppercase tracking-wider text-[var(--color-text-muted)]">
            <th className="px-4 py-2 text-left font-medium">Symbol</th>
            <th className="px-4 py-2 text-right font-medium">Qty</th>
            <th className="px-4 py-2 text-right font-medium">Cost</th>
            <th className="px-4 py-2 text-right font-medium">Value</th>
            <th className="px-4 py-2 text-right font-medium">P&amp;L</th>
            <th className="px-4 py-2 text-right font-medium">Return</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-[var(--color-border)]">
          {positions.map((p) => {
            const cost = costOf(p);
            const ret = cost > 0 ? p.unrealized_pl / cost : 0;
            return (
              <tr
                key={p.symbol}
                onClick={() => onSelect(p.symbol)}
                className="cursor-pointer hover:bg-[var(--color-surface-hover)]"
              >
                <td className="px-4 py-2 font-medium text-[var(--color-text)]">{p.symbol}{p.valuation_adjusted && <span className="ml-2 text-[10px] font-normal text-[var(--color-primary)]">split estimate</span>}</td>
                <td className="px-4 py-2 text-right tabular-nums text-[var(--color-text-muted)]">
                  {p.qty.toLocaleString(undefined, { maximumFractionDigits: 4 })}
                </td>
                <td className="px-4 py-2 text-right tabular-nums text-[var(--color-text-muted)]">
                  {usd(cost)}
                </td>
                <td className="px-4 py-2 text-right tabular-nums text-[var(--color-text)]">
                  {usd(p.market_value)}
                </td>
                <td className={`px-4 py-2 text-right tabular-nums ${tone(p.unrealized_pl)}`}>
                  {signedUsd(p.unrealized_pl)}
                </td>
                <td className={`px-4 py-2 text-right tabular-nums ${tone(ret)}`}>{signedPct(ret)}</td>
              </tr>
            );
          })}
        </tbody>
        <tfoot>
          <tr className="border-t border-[var(--color-border)] font-semibold">
            <td className="px-4 py-2 text-[var(--color-text)]">Total</td>
            <td />
            <td className="px-4 py-2 text-right tabular-nums text-[var(--color-text-muted)]">
              {usd(totalCost)}
            </td>
            <td className="px-4 py-2 text-right tabular-nums text-[var(--color-text)]">
              {usd(totalValue)}
            </td>
            <td className={`px-4 py-2 text-right tabular-nums ${tone(totalPl)}`}>
              {signedUsd(totalPl)}
            </td>
            <td
              className={`px-4 py-2 text-right tabular-nums ${tone(totalCost > 0 ? totalPl / totalCost : 0)}`}
            >
              {signedPct(totalCost > 0 ? totalPl / totalCost : 0)}
            </td>
          </tr>
        </tfoot>
      </table>
    </div>
  );
}
