import { useQuery } from "@tanstack/react-query";

import { api, errorStatus } from "@/lib/api";

/**
 * The shared paper account's equity/cash/buying-power and whether the
 * market is open. Admin-only on the backend — one shared Alpaca account for
 * the install, not per-user — so a non-admin viewer gets a quiet, specific
 * notice here instead of a generic error.
 */
export function EquityAccountSummary() {
  const accountQuery = useQuery({
    queryKey: ["equity-account"],
    queryFn: api.getEquityAccount,
    retry: (count, err) => errorStatus(err) !== 403 && count < 2,
  });
  const statusQuery = useQuery({
    queryKey: ["equity-market-status"],
    queryFn: api.getEquityMarketStatus,
    retry: (count, err) => errorStatus(err) !== 403 && count < 2,
    refetchInterval: 60_000,
  });

  if (errorStatus(accountQuery.error) === 403) {
    return (
      <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] px-4 py-3 text-sm text-[var(--color-text-muted)]">
        Admin access is required to view the paper account.
      </div>
    );
  }

  const account = accountQuery.data;
  const isOpen = statusQuery.data?.is_open;

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] px-4 py-3">
      {account?.valuation_note && <p className="mb-3 border-l-2 border-[var(--color-primary)] pl-3 text-xs text-[var(--color-text)]">{account.valuation_note}</p>}
      <div className="flex flex-wrap items-center gap-6">
        <Stat label={account?.valuation_adjusted ? "Estimated equity" : "Equity"} value={account?.equity ?? undefined} loading={accountQuery.isLoading} />
        <Stat label="Cash" value={account?.cash} loading={accountQuery.isLoading} />
        <Stat
          label="Broker buying power"
          value={account?.buying_power}
          loading={accountQuery.isLoading}
        />
        <div className="ml-auto flex items-center gap-1.5 text-xs">
          <span
            className={`h-2 w-2 rounded-full ${
              isOpen ? "bg-[var(--color-green)]" : "bg-[var(--color-text-muted)]"
            }`}
          />
          <span className="text-[var(--color-text-muted)]">
            {statusQuery.isLoading
              ? "Checking market…"
              : isOpen
                ? "Market open"
                : "Market closed"}
          </span>
        </div>
      </div>
    </div>
  );
}

function Stat({
  label,
  value,
  loading,
}: {
  label: string;
  value: number | undefined;
  loading: boolean;
}) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)]">
        {label}
      </div>
      <div className="text-sm font-semibold text-[var(--color-text)]">
        {loading || value == null
          ? "—"
          : value.toLocaleString(undefined, {
              style: "currency",
              currency: "USD",
              maximumFractionDigits: 2,
            })}
      </div>
    </div>
  );
}
