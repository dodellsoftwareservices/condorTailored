/** @vitest-environment jsdom */
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import { Portfolio } from "./Portfolio";

const { snapshot } = vi.hoisted(() => ({
  snapshot: (variant: string) => ({
    equity: "50000", cash: variant === "mm" ? "49750" : "50000", start_cash: "50000",
    gross_exposure: "250", open_orders: 0, volume_30d: "250", total_return_pct: "0",
    positions: variant === "hodl" ? [] : [{ asset: variant === "mm" ? "ETH" : "BTC", qty: variant === "mm" ? "0.1" : "-0.01", side: "short", avg_cost: "2500", price: "2500", value: "250", unrealized_pnl: "0" }],
  }),
}));

vi.mock("@tanstack/react-query", () => ({
  keepPreviousData: (x: unknown) => x,
  useQueryClient: () => ({ prefetchQuery: vi.fn(), setQueryData: vi.fn() }),
  useQuery: ({ queryKey: k, enabled }: { queryKey: string[]; enabled?: boolean }) => {
    let data: unknown;
    if (enabled !== false) {
      if (k[0] === "portfolio") data = { total_usd: 240, connectors: [{ connector: "kraken", total_usd: 240, balances: [{ token: "XMR", total: 0.4, available: 0.4, usd_value: 240 }] }] };
      else if (k[0] === "sim-kraken" && k[2] === "portfolio") data = snapshot(k[1]);
      else if (k[0] === "sim-kraken" && k[2] === "status") data = { running: true };
      else if (k[0] === "bots") data = { bots: [], controllers: [] };
      else if (k[0] === "consolidated-positions") data = { executor_positions: [], bot_positions: [] };
      else if (k[0].startsWith("portfolio-history")) data = { points: [] };
      else if (k[0] === "agents") data = [{ slug: "sim_crypto_trader", status: "running", instances: [{ status: "running", frequency_sec: 3600 }] }, { slug: "sim_futures_trader", status: "running", instances: [{ status: "running", frequency_sec: 900 }] }];
      else if (k[0] === "executors") data = [];
    }
    return { data, isLoading: false, isPlaceholderData: false, error: null };
  },
}));
vi.mock("@/hooks/useServer", () => ({ useServer: () => ({ server: "local" }) }));
vi.mock("@/hooks/useWebSocket", () => ({ useCondorWebSocket: () => {} }));
vi.mock("@/hooks/useLpPositions", () => ({ useLpPositions: () => ({ positions: [], label: () => "", isLoading: false }) }));
vi.mock("@/hooks/useRates", () => ({ useRates: () => ({ convert: (value: number) => ({ value, converted: true }), formatValueDetailed: (value: number) => `$${value}`, formatPnlValue: (value: number) => `$${value}`, resolvedSymbol: "$" }) }));
vi.mock("@/lib/queryClient", () => ({ EXECUTORS_REFETCH_MS: 30000, agentsQuery: () => ({ queryKey: ["agents"] }), executorsQuery: () => ({ queryKey: ["executors"] }) }));

let cleanup: (() => void) | undefined;
afterEach(() => { cleanup?.(); vi.unstubAllGlobals(); });

it("uses only Assets and Positions, with all simulator accounts alongside real Kraken", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  cleanup = () => { act(() => root.unmount()); host.remove(); };
  await act(async () => { root.render(<MemoryRouter><Portfolio /></MemoryRouter>); });
  const tabs = [...host.querySelectorAll<HTMLButtonElement>('[role="tab"]')];
  expect(tabs.map((t) => t.textContent)).toEqual(["Assets", "Positions(2)"]);
  const assets = host.querySelector('[aria-label="Portfolio assets, scroll horizontally for all columns"]')!;
  for (const label of ["kraken", "XMR", "krakenSimHodl", "krakenSimMM", "krakenSimFutures", "USD collateral"]) expect(assets.textContent).toContain(label);
  expect(assets.textContent).not.toContain("binance_perpetual_testnet");
  expect(assets.textContent).not.toContain("BTC"); // futures exposure is not an asset
  expect(assets.textContent).toContain("Running · hourly reviews");
  expect(assets.textContent).toContain("Running · 15m reviews");
  expect(host.querySelector('[aria-label="Portfolio summary"]')?.textContent).toContain("$240.00");
  await act(async () => tabs[1].click());
  const rows = [...host.querySelectorAll('[data-hold-row]')];
  expect(rows).toHaveLength(2);
  expect(rows.every((r) => r.textContent?.includes("Simulated · no real funds"))).toBe(true);
  expect(rows.some((r) => r.textContent?.includes("SHORT"))).toBe(true);
});
