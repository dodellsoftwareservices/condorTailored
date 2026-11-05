/**
 * The krakenSimHodl / krakenSimMM tab contents on the Portfolio page.
 *
 * @vitest-environment jsdom
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import { SimAccountTab } from "./SimAccountTab";
import type { SimVariant } from "./simAccountData";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let host: HTMLElement | null = null;

async function mount(variant: SimVariant) {
  host = document.createElement("div");
  document.body.appendChild(host);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  root = createRoot(host);
  await act(async () => {
    root!.render(
      <QueryClientProvider client={qc}>
        <SimAccountTab variant={variant} />
      </QueryClientProvider>,
    );
  });
  await act(async () => { await new Promise((r) => setTimeout(r, 20)); });
  return host;
}

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
  vi.restoreAllMocks();
});

const costs = {
  net_pnl_after_all_costs: "-2.30", equity_pnl: "0", traded_volume: "500", fills: { taker: 0, maker: 2 },
  costs: { exchange_fees: "2.00", slippage_vs_arrival_mid: "0", llm_requests: 0, llm_cost: "0", fixed_running_cost: "0", total_execution_and_operating: "2.30" },
};

describe("SimAccountTab", () => {
  it("lists the cash and each coin position with cost, price, value and P&L", async () => {
    vi.spyOn(api, "getSimPortfolio").mockResolvedValue({
      equity: "100000.00", cash: "99500.00", start_cash: "100000", total_return_pct: "0.00",
      gross_exposure: "500.00", open_orders: 1, volume_30d: "500",
      positions: [{ asset: "BTC", qty: "0.00625", avg_cost: "80000", price: "80100", value: "500.62", unrealized_pnl: "0.62" }],
    });
    vi.spyOn(api, "getSimCosts").mockResolvedValue(costs);
    const el = await mount("hodl");
    expect(el.textContent).toContain("krakenSimHodl");
    expect(el.textContent).toContain("Simulated · no real funds");
    expect(el.querySelector("[data-testid=sim-cash-row]")?.textContent).toContain("$99,500.00");
    const row = el.querySelector("[data-testid=sim-position-row]")!;
    expect(row.textContent).toContain("BTC");
    expect(row.textContent).toContain("0.00625");
    expect(row.textContent).toContain("$80,000.00");
    expect(row.textContent).toContain("+$0.62");
  });

  it("market maker tab shows its status and resting quotes, and says why it has no inventory", async () => {
    vi.spyOn(api, "getSimMmPortfolio").mockResolvedValue({
      equity: "100000.00", cash: "100000.00", start_cash: "100000", total_return_pct: "0.00",
      gross_exposure: "0", open_orders: 3, volume_30d: "0", positions: [],
    });
    vi.spyOn(api, "getSimMmCosts").mockResolvedValue({ ...costs, net_pnl_after_all_costs: "0", fills: { taker: 0, maker: 0 } });
    vi.spyOn(api, "getSimMmStatus").mockResolvedValue({
      running: true, configured: true, cycles: 4, quotes_placed: 3, quotes_cancelled: 0, maker_fills: 0, volume: "0",
      pairs: { "BTC-USD": { mid: "80400", half_spread_bps: "45", maker_fee_bps: "40", bid: "80038.2", ask: null, inventory: "0" } },
    });
    const el = await mount("mm");
    expect(el.textContent).toContain("krakenSimMM");
    expect(el.querySelector("[data-testid=sim-mm-status]")?.textContent).toContain("Quoting");
    expect(el.querySelector("[data-testid=sim-quote-row]")?.textContent).toContain("$80,038.20");
    expect(el.textContent).toContain("No inventory yet");
  });

  it("shows a small loss and explains a flat account that has traded", async () => {
    vi.spyOn(api, "getSimPortfolio").mockResolvedValue({
      equity: "99995.66", cash: "99995.66", start_cash: "100000", total_return_pct: "-0.00",
      gross_exposure: "0", open_orders: 0, volume_30d: "999.61", positions: [],
    });
    vi.spyOn(api, "getSimCosts").mockResolvedValue({
      ...costs, net_pnl_after_all_costs: "-4.34", fills: { taker: 0, maker: 6 },
      costs: { ...costs.costs, exchange_fees: "4.00" },
      recent_executed_orders: [
        { order_id: "sell-1", client_order_id: "cal-sell", pair: "BTC-USD", side: "sell", last_fill_ts: 1793821318,
          fill_count: 1, qty: "0.00312", notional: "249.94", fee: "1.00", realized_pnl: "-2.04", calibration: true },
        { order_id: "buy-1", client_order_id: "cal-buy", pair: "BTC-USD", side: "buy", last_fill_ts: 1793820277,
          fill_count: 3, qty: "0.00312", notional: "249.98", fee: "1.00", realized_pnl: "0", calibration: true },
      ],
    });
    const el = await mount("hodl");
    expect(el.textContent).toContain("-0.0043%");
    expect(el.textContent).toContain("-$4.34");
    expect(el.textContent).toContain("earlier buys were sold");
    const orders = el.querySelectorAll("[data-testid=sim-executed-order]");
    expect(orders).toHaveLength(2);
    expect(orders[0].textContent).toContain("sell");
    expect(orders[0].textContent).toContain("Calibration");
    expect(orders[1].textContent).toContain("buy · 3 fills");
  });

  it("futures tab shows shorts with leverage and funding, and labels the collateral", async () => {
    vi.spyOn(api, "getSimFutPortfolio").mockResolvedValue({
      equity: "49999.57", cash: "50000.00", start_cash: "50000", total_return_pct: "-0.00", gross_exposure: "800.34",
      open_orders: 0, volume_30d: "800", leverage: "0.02", funding_paid: "-0.12", liquidations: 0,
      positions: [{ asset: "BTC", side: "short", qty: "-0.0100", avg_cost: "80031", price: "80033.79", value: "800.34", unrealized_pnl: "-0.03" }],
    });
    vi.spyOn(api, "getSimFutCosts").mockResolvedValue({ ...costs, net_pnl_after_all_costs: "-0.43", fills: { taker: 1, maker: 0 } });
    const el = await mount("fut");
    expect(el.textContent).toContain("krakenSimFutures");
    expect(el.textContent).toContain("0.02x");
    expect(el.querySelector("[data-testid=sim-cash-row]")?.textContent).toContain("USD collateral");
    const row = el.querySelector("[data-testid=sim-position-row]")!;
    expect(row.textContent).toContain("short");
    expect(row.textContent).toContain("-0.01");
  });

  it("renders nothing when the route refuses the caller", async () => {
    vi.spyOn(api, "getSimPortfolio").mockRejectedValue(new Error("403"));
    vi.spyOn(api, "getSimCosts").mockRejectedValue(new Error("403"));
    const el = await mount("hodl");
    expect(el.querySelector("[data-testid=sim-account-tab]")).toBeNull();
  });
});
