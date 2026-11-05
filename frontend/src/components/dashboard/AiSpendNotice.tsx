import { useQuery } from "@tanstack/react-query";
import { Cpu } from "lucide-react";

import { api } from "@/lib/api";

const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 2 });

/** Below this share of the cap left, the line turns to the attention colour. */
export const LOW_HEADROOM_SHARE = 0.2;

/**
 * Where the paid-model spend stands against the monthly cap. Renders nothing when no cap
 * is configured or the read fails, so it can never get in the way of the page.
 */
export function AiSpendNotice({ enabled }: { enabled: boolean }) {
  const spend = useQuery({
    queryKey: ["llm-spend"],
    queryFn: () => api.getLlmSpend(),
    enabled,
    refetchInterval: 60_000,
    staleTime: 30_000,
    retry: false,
  });
  const data = spend.data;
  if (!data || !data.enabled) return null;

  if (data.status === "paused") {
    return (
      <div className="dashboard-notice dashboard-spend is-low" role="status" data-testid="ai-spend">
        <Cpu size={13} aria-hidden /> <strong>Paid AI requests are paused.</strong> {data.reason ?? "The spend baseline could not be read."} Free models keep running.
      </div>
    );
  }
  const counted = data.counted_usd ?? 0;
  const left = data.headroom_usd ?? 0;
  const share = data.cap_usd > 0 ? Math.min(Math.max(counted / data.cap_usd, 0), 1) : 0;
  const low = data.cap_usd > 0 && left / data.cap_usd < LOW_HEADROOM_SHARE;
  return (
    <div className={`dashboard-notice dashboard-spend${low ? " is-low" : ""}`} role="status" data-testid="ai-spend">
      <Cpu size={13} aria-hidden /> <strong>AI model spend this month</strong> {usd.format(counted)} of {usd.format(data.cap_usd)} · {usd.format(left)} left
      <span className="dashboard-spend-track" role="img" aria-label={`${Math.round(share * 100)}% of the monthly AI budget used`}>
        <span style={{ width: `${share * 100}%` }} />
      </span>
      {!!data.unpriced_responses && <small> · {data.unpriced_responses} response{data.unpriced_responses === 1 ? "" : "s"} counted at the worst case</small>}
    </div>
  );
}
