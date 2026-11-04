import { useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api";

export type SimVariant = "hodl" | "mm" | "fut";

/** Tab titles are the account names, exactly as the owner named them. */
export const SIM_TAB_LABEL: Record<SimVariant, string> = { hodl: "krakenSimHodl", mm: "krakenSimMM", fut: "krakenSimFutures" };
export const SIM_TAB_BLURB: Record<SimVariant, string> = {
  hodl: "Long / hedge account run by the AI agent",
  mm: "Arb / HFT market-making account",
  fut: "Perpetual futures: shorts, hedges and relative value",
};

/** One simulated Kraken account as data the tab and its badge can use. */
export function useSimAccount(variant: SimVariant) {
  const mm = variant === "mm";
  const portfolio = useQuery({
    queryKey: ["sim-kraken", variant, "portfolio"],
    queryFn: () => (variant === "fut" ? api.getSimFutPortfolio() : mm ? api.getSimMmPortfolio() : api.getSimPortfolio()),
    refetchInterval: 15000,
    retry: false,
  });
  const costs = useQuery({
    queryKey: ["sim-kraken", variant, "costs"],
    queryFn: () => (variant === "fut" ? api.getSimFutCosts() : mm ? api.getSimMmCosts() : api.getSimCosts()),
    refetchInterval: 15000,
    retry: false,
  });
  const maker = useQuery({
    queryKey: ["sim-kraken", "mm", "status"],
    queryFn: () => api.getSimMmStatus(),
    refetchInterval: 10000,
    retry: false,
    enabled: mm,
  });
  return { pf: portfolio.data, costs: costs.data, maker: maker.data };
}
