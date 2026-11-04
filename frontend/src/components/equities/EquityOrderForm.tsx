import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, errorCode, errorStatus } from "@/lib/api";

export interface EquityOrderFormProps {
  symbol: string;
}

/**
 * One paper market order: side, a notional amount, submit. No preview step,
 * no limit/share-quantity orders — see condor/web/routes/paper_equities.py's
 * own module docstring for what this milestone deliberately leaves out.
 *
 * Disables itself from `rollout-status` rather than waiting to discover a
 * disabled feature from a 503 on submit. A 403 (this account is admin-only)
 * and `equity_order_ambiguous` (a timeout — the order may exist) each get
 * their own message; the backend's own `equity_order_too_large` message is
 * specific enough to show as-is.
 */
export function EquityOrderForm({ symbol }: EquityOrderFormProps) {
  const queryClient = useQueryClient();
  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [notional, setNotional] = useState("100");

  const rolloutQuery = useQuery({
    queryKey: ["equity-rollout-status"],
    queryFn: api.getEquityRolloutStatus,
  });

  const submit = useMutation({
    mutationFn: () => api.submitEquityOrder({ symbol, notional: Number(notional), side }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["equity-positions"] });
      queryClient.invalidateQueries({ queryKey: ["equity-account"] });
    },
  });

  const submissionsDisabled =
    rolloutQuery.data != null && !rolloutQuery.data.paper_submissions_enabled;
  const amount = Number(notional);
  const canSubmit = submit.isIdle || submit.isError;

  return (
    <div className="space-y-3 rounded-lg border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
      <div className="flex gap-1 rounded-lg border border-[var(--color-border)] p-1">
        {(["buy", "sell"] as const).map((s) => (
          <button
            key={s}
            type="button"
            onClick={() => setSide(s)}
            className={`flex-1 rounded-md px-3 py-1.5 text-xs font-medium capitalize transition-colors ${
              side === s
                ? s === "buy"
                  ? "bg-[var(--color-green)]/15 text-[var(--color-green)]"
                  : "bg-[var(--color-red)]/15 text-[var(--color-red)]"
                : "text-[var(--color-text-muted)] hover:bg-[var(--color-surface-hover)]"
            }`}
          >
            {s}
          </button>
        ))}
      </div>

      <div>
        <label className="mb-1 block text-xs text-[var(--color-text-muted)]" htmlFor="equity-order-notional">
          Amount (USD)
        </label>
        <input
          id="equity-order-notional"
          type="number"
          min={1}
          step={1}
          value={notional}
          onChange={(e) => setNotional(e.target.value)}
          className="w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2 text-sm text-[var(--color-text)] outline-none focus:border-[var(--color-primary)]"
        />
      </div>

      {submissionsDisabled && (
        <p className="text-xs text-[var(--color-text-muted)]">
          Paper trading is not enabled on this install.
        </p>
      )}

      <button
        type="button"
        disabled={submissionsDisabled || !canSubmit || !(amount > 0)}
        onClick={() => submit.mutate()}
        className={`w-full rounded-lg px-4 py-2 text-sm font-semibold text-white transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${
          side === "buy" ? "bg-[var(--color-green)] hover:opacity-90" : "bg-[var(--color-red)] hover:opacity-90"
        }`}
      >
        {submit.isPending ? "Submitting…" : `${side === "buy" ? "Buy" : "Sell"} ${symbol}`}
      </button>

      {submit.isSuccess && (
        <p className="text-xs text-[var(--color-green)]">
          Order {submit.data.status === "accepted" ? "accepted" : submit.data.status}.
        </p>
      )}
      {submit.isError && <OrderError error={submit.error} />}
    </div>
  );
}

function OrderError({ error }: { error: unknown }) {
  const code = errorCode(error);
  if (code === "equity_order_ambiguous") {
    return (
      <p className="rounded-md bg-[var(--color-red)]/10 px-3 py-2 text-xs font-medium text-[var(--color-red)]">
        Uncertain whether this order went through — check Positions before
        submitting again. Do not retry blindly.
      </p>
    );
  }
  if (errorStatus(error) === 403) {
    return (
      <p className="text-xs text-[var(--color-text-muted)]">
        Admin access is required to submit paper orders.
      </p>
    );
  }
  return <p className="text-xs text-[var(--color-red)]">{(error as Error).message}</p>;
}
