import { useQuery } from "@tanstack/react-query";

import { api, type ConnectorBalance, type ConsolidatedPosition, type SimPortfolio } from "@/lib/api";

export type SimVariant = "hodl" | "mm" | "fut";

/** Tab titles are the account names, exactly as the owner named them. */
export const SIM_TAB_LABEL: Record<SimVariant, string> = { hodl: "krakenSimHodl", mm: "krakenSimMM", fut: "krakenSimFutures" };
export const SIM_TAB_BLURB: Record<SimVariant, string> = {
  hodl: "Long / hedge account run by the AI agent",
  mm: "Arb / HFT market-making account",
  fut: "Perpetual futures: shorts, hedges and relative value",
};

/** One simulated Kraken account as data the tab and its badge can use. */
export function useSimAccount(variant: SimVariant, details = true) {
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
    enabled: details,
  });
  const maker = useQuery({
    queryKey: ["sim-kraken", "mm", "status"],
    queryFn: () => api.getSimMmStatus(),
    refetchInterval: 10000,
    retry: false,
    enabled: mm,
  });
  return { pf: portfolio.data, costs: costs.data, maker: maker.data, isLoading: portfolio.isLoading, error: portfolio.error };
}

export type PortfolioPosition = ConsolidatedPosition & { simulated?: boolean };
export type SimAssetGroup = ConnectorBalance & { simulated: true; variant: SimVariant };

/** Futures are collateral here; their contract exposure belongs in Positions. */
export function simAssetGroup(variant: SimVariant, pf: SimPortfolio): SimAssetGroup {
  const cash = Number(pf.cash);
  const balances = [{ token: variant === "fut" ? "USD collateral" : "USD", total: cash, available: cash, usd_value: cash }];
  if (variant !== "fut") {
    for (const p of pf.positions) {
      if (Number(p.qty) !== 0) balances.push({ token: p.asset, total: Number(p.qty), available: Number(p.qty), usd_value: Number(p.value) });
    }
  }
  return { connector: SIM_TAB_LABEL[variant], balances, total_usd: balances.reduce((sum, b) => sum + b.usd_value, 0), simulated: true, variant };
}

export function simPositions(variant: SimVariant, pf: SimPortfolio): PortfolioPosition[] {
  return pf.positions.filter((p) => Number(p.qty) !== 0).map((p) => ({
    connector_name: SIM_TAB_LABEL[variant],
    trading_pair: `${p.asset}-USD`,
    position_side: variant === "fut" ? (p.side ?? (Number(p.qty) < 0 ? "short" : "long")).toUpperCase() : "BUY",
    amount: Math.abs(Number(p.qty)),
    entry_price: Number(p.avg_cost ?? 0),
    current_price: Number(p.price ?? p.mid ?? (Number(p.value) / Math.abs(Number(p.qty)))),
    notional_value: Number(p.value ?? 0),
    unrealized_pnl: Number(p.unrealized_pnl ?? 0),
    realized_pnl: 0,
    cum_fees: 0,
    executor_count: 0,
    leverage: 0,
    controller_id: "",
    source: "bot",
    source_name: SIM_TAB_LABEL[variant],
    simulated: true,
  }));
}
