import { describe, expect, it } from "vitest";
import type { SimPortfolio } from "@/lib/api";
import { simAssetGroup, simPositions } from "./simAccountData";

const portfolio: SimPortfolio = {
  equity: "49995", cash: "49000", start_cash: "50000", total_return_pct: "-0.01",
  gross_exposure: "995", open_orders: 0, volume_30d: "1000",
  positions: [{ asset: "ETH", qty: "0.5", avg_cost: "2000", mid: "1990", value: "995", unrealized_pnl: "-5" }],
};

describe("simulated accounts in the portfolio tables", () => {
  it("groups spot cash and coins by the simulator account", () => {
    const group = simAssetGroup("mm", portfolio);
    expect(group.connector).toBe("krakenSimMM");
    expect(group.simulated).toBe(true);
    expect(group.balances.map((b) => b.token)).toEqual(["USD", "ETH"]);
    expect(group.total_usd).toBe(49995);
    expect(simPositions("mm", portfolio)[0]).toMatchObject({ simulated: true, current_price: 1990, position_side: "BUY", unrealized_pnl: -5 });
  });

  it("keeps futures notional out of assets and preserves short direction in positions", () => {
    const futures = { ...portfolio, positions: [{ ...portfolio.positions[0], qty: "-0.5", side: "short" as const }] };
    const group = simAssetGroup("fut", futures);
    expect(group.balances.map((b) => b.token)).toEqual(["USD collateral"]);
    expect(group.total_usd).toBe(49000);
    expect(simPositions("fut", futures)[0]).toMatchObject({ connector_name: "krakenSimFutures", amount: 0.5, position_side: "SHORT", notional_value: 995 });
  });
});
