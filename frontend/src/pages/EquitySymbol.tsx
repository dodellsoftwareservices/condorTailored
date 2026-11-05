import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";

import { EquityAccountSummary } from "@/components/equities/EquityAccountSummary";
import { EquityChart } from "@/components/equities/EquityChart";
import { EquityOrderForm } from "@/components/equities/EquityOrderForm";
import { api, errorStatus } from "@/lib/api";

const PERIOD_OPTIONS = [
  { value: "5d", label: "5D" },
  { value: "1mo", label: "1M" },
  { value: "3mo", label: "3M" },
  { value: "6mo", label: "6M" },
  { value: "1y", label: "1Y" },
] as const;

/** Period → the interval that keeps the candle count reasonable at that span. */
const INTERVAL_FOR_PERIOD: Record<string, string> = {
  "5d": "15m",
  "1mo": "1h",
  "3mo": "1d",
  "6mo": "1d",
  "1y": "1d",
};

/** The quote is polled, not streamed — WebSocket is explicitly out of scope
 * for this milestone (see condor/web/routes/equities.py's own docstring). */
const QUOTE_POLL_MS = 10_000;

/**
 * One equity symbol: instrument header, quote, chart, order form —
 * URL-synced (`useParams`, mirroring `DexPool.tsx`) so the page is
 * reload-safe and shareable.
 */
export function EquitySymbol() {
  const { symbol = "" } = useParams<{ symbol: string }>();
  const [period, setPeriod] = useState<(typeof PERIOD_OPTIONS)[number]["value"]>("3mo");
  const interval = INTERVAL_FOR_PERIOD[period] ?? "1d";

  const instrumentQuery = useQuery({
    queryKey: ["equity-instrument", symbol],
    queryFn: () => api.getEquityInstrument(symbol),
    enabled: !!symbol,
  });

  const quoteQuery = useQuery({
    queryKey: ["equity-quote", symbol],
    queryFn: () => api.getEquityQuote(symbol),
    enabled: !!symbol,
    refetchInterval: QUOTE_POLL_MS,
  });

  const barsQuery = useQuery({
    queryKey: ["equity-bars", symbol, period, interval],
    queryFn: () => api.getEquityBars(symbol, period, interval),
    enabled: !!symbol,
  });

  // Admin-only on the backend, same as the account summary — a non-admin
  // viewer simply sees no cost-basis line rather than an error here; the
  // order form and account summary already say plainly that admin access
  // is needed.
  const positionsQuery = useQuery({
    queryKey: ["equity-positions"],
    queryFn: api.getEquityPositions,
    enabled: !!symbol,
    retry: (count, err) => errorStatus(err) !== 403 && count < 2,
  });
  const position = positionsQuery.data?.find((p) => p.symbol === symbol.toUpperCase());
  // AlpacaPaperClient.get_positions() returns market_value and unrealized_pl,
  // not a cost-basis price directly — derived here rather than added to the
  // backend model for one chart line. market_value - unrealized_pl is
  // approximately what was paid; dividing by qty gives the per-share price.
  const costBasis =
    position && position.qty !== 0
      ? (position.market_value - position.unrealized_pl) / position.qty
      : null;

  if (instrumentQuery.isError && errorStatus(instrumentQuery.error) === 404) {
    return (
      <div className="mx-auto max-w-2xl p-6">
        <Link to="/equities" className="text-sm text-[var(--color-primary)] hover:underline">
          ← Back to search
        </Link>
        <p className="mt-4 text-sm text-[var(--color-text-muted)]">
          No instrument found for "{symbol}".
        </p>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-5xl space-y-4 p-6">
      <Link to="/equities" className="text-sm text-[var(--color-primary)] hover:underline">
        ← Back to search
      </Link>

      <div className="flex items-baseline justify-between">
        <div>
          <h1 className="text-xl font-semibold text-[var(--color-text)]">
            {symbol.toUpperCase()}
          </h1>
          <p className="text-sm text-[var(--color-text-muted)]">
            {instrumentQuery.data?.name || "—"}
          </p>
        </div>
        <QuotePanel
          bid={quoteQuery.data?.bid}
          ask={quoteQuery.data?.ask}
          loading={quoteQuery.isLoading}
        />
      </div>

      <EquityAccountSummary />

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[1fr_320px]">
        <div className="space-y-2">
          <div className="flex gap-1">
            {PERIOD_OPTIONS.map((p) => (
              <button
                key={p.value}
                type="button"
                onClick={() => setPeriod(p.value)}
                className={`rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
                  period === p.value
                    ? "bg-[var(--color-primary)]/15 text-[var(--color-primary)]"
                    : "text-[var(--color-text-muted)] hover:bg-[var(--color-surface-hover)]"
                }`}
              >
                {p.label}
              </button>
            ))}
          </div>
          <div className="h-[340px] rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-2">
            <EquityChart bars={barsQuery.data?.bars ?? []} costBasis={costBasis} />
          </div>
        </div>

        <EquityOrderForm symbol={symbol.toUpperCase()} />
      </div>
    </div>
  );
}

function QuotePanel({
  bid,
  ask,
  loading,
}: {
  bid: number | undefined;
  ask: number | undefined;
  loading: boolean;
}) {
  return (
    <div className="text-right">
      <div className="text-lg font-semibold tabular-nums text-[var(--color-text)]">
        {loading || bid == null ? "—" : `$${bid.toFixed(2)} / $${ask?.toFixed(2)}`}
      </div>
      {/* Explicitly labeled as polling, not live — WebSocket streaming is out
          of scope this milestone; saying "live" here would be exactly the
          kind of delayed-poll-as-realtime claim the integration plan warned
          against. */}
      <div className="text-[10px] text-[var(--color-text-muted)]">
        bid / ask · polling every {QUOTE_POLL_MS / 1000}s
      </div>
    </div>
  );
}
