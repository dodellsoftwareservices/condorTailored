import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";

import { EquityAccountSummary } from "@/components/equities/EquityAccountSummary";
import { EquityPositions } from "@/components/equities/EquityPositions";
import { api } from "@/lib/api";

const SEARCH_DEBOUNCE_MS = 400;

/**
 * Symbol search, the entry point into paper equities — mirrors Dex.tsx's own
 * shape (plain state, not URL-synced; a result click navigates into a
 * URL-addressable detail page). No useServer()/NoServerCard gating: there's
 * no Hummingbot server concept here, one shared Alpaca paper account serves
 * the whole install.
 */
export function Equities() {
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");

  useEffect(() => {
    const t = setTimeout(() => setDebouncedQuery(query.trim()), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [query]);

  const searchQuery = useQuery({
    queryKey: ["equity-search", debouncedQuery],
    queryFn: () => api.searchEquities(debouncedQuery),
    enabled: debouncedQuery.length > 0,
  });

  return (
    <div className="mx-auto max-w-2xl space-y-6 p-6">
      <div>
        <h1 className="text-lg font-semibold text-[var(--color-text)]">Equities</h1>
        <p className="text-sm text-[var(--color-text-muted)]">
          Paper trading via Alpaca. Research data from Yahoo Finance.
        </p>
      </div>

      <EquityAccountSummary />

      <EquityPositions onSelect={(symbol) => navigate(`/equities/${encodeURIComponent(symbol)}`)} />

      <input
        type="text"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder="Search a symbol or company — e.g. AAPL"
        className="w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] px-4 py-2.5 text-sm text-[var(--color-text)] outline-none focus:border-[var(--color-primary)]"
        autoFocus
      />

      {searchQuery.isLoading && debouncedQuery && (
        <p className="text-sm text-[var(--color-text-muted)]">Searching…</p>
      )}

      {searchQuery.data && searchQuery.data.results.length === 0 && (
        <p className="text-sm text-[var(--color-text-muted)]">
          No matches for "{debouncedQuery}".
        </p>
      )}

      {searchQuery.data && searchQuery.data.results.length > 0 && (
        <div className="divide-y divide-[var(--color-border)] overflow-hidden rounded-lg border border-[var(--color-border)]">
          {searchQuery.data.results.map((r) => (
            <button
              key={r.symbol}
              type="button"
              onClick={() => navigate(`/equities/${encodeURIComponent(r.symbol)}`)}
              className="flex w-full items-center justify-between px-4 py-3 text-left hover:bg-[var(--color-surface-hover)]"
            >
              <div>
                <div className="text-sm font-medium text-[var(--color-text)]">{r.symbol}</div>
                <div className="text-xs text-[var(--color-text-muted)]">
                  {r.name || r.exchange}
                </div>
              </div>
              <span className="text-xs text-[var(--color-text-muted)]">{r.quote_type}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
