/**
 * @vitest-environment jsdom
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { EquityPosition } from "@/lib/api";
import { EquityPositions } from "./EquityPositions";

const getEquityPositions = vi.fn<() => Promise<EquityPosition[]>>();

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, api: { getEquityPositions: () => getEquityPositions() } };
});

declare global {
  // eslint-disable-next-line no-var
  var IS_REACT_ACT_ENVIRONMENT: boolean;
}

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  getEquityPositions.mockReset();
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

async function render(onSelect: (s: string) => void = () => {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root.render(
      <QueryClientProvider client={client}>
        <EquityPositions onSelect={onSelect} />
      </QueryClientProvider>,
    );
  });
  for (let i = 0; i < 50 && /Loading positions/.test(container.textContent ?? ""); i++) {
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
  }
}

describe("EquityPositions", () => {
  it("derives cost and return from market value and unrealized P&L, with totals", async () => {
    getEquityPositions.mockResolvedValue([
      { symbol: "AAA", qty: 2, market_value: 2750, unrealized_pl: 250 }, // cost 2500, +10%
      { symbol: "BBB", qty: 5, market_value: 2375, unrealized_pl: -125 }, // cost 2500, -5%
    ]);
    await render();
    const text = container.textContent ?? "";
    expect(text).toContain("Open positions (2)");
    expect(text).toContain("+10.00%");
    expect(text).toContain("−5.00%");
    // Totals: cost 5000, value 5125, P&L +125, +2.50%
    expect(text).toContain("$5,000.00");
    expect(text).toContain("$5,125.00");
    expect(text).toContain("+$125.00");
    expect(text).toContain("+2.50%");
  });

  it("lists the largest position first", async () => {
    getEquityPositions.mockResolvedValue([
      { symbol: "SMALL", qty: 1, market_value: 100, unrealized_pl: 0 },
      { symbol: "BIG", qty: 1, market_value: 900, unrealized_pl: 0 },
    ]);
    await render();
    const symbols = [...container.querySelectorAll("tbody tr td:first-child")].map((td) => td.textContent);
    expect(symbols).toEqual(["BIG", "SMALL"]);
  });

  it("says so when there are no open positions", async () => {
    getEquityPositions.mockResolvedValue([]);
    await render();
    expect(container.textContent).toContain("No open positions.");
  });

  it("renders nothing for a non-admin; the account summary already explains the 403", async () => {
    getEquityPositions.mockRejectedValue(Object.assign(new Error("forbidden"), { status: 403 }));
    await render();
    expect(container.textContent).toBe("");
  });

  it("calls onSelect with the symbol when a row is clicked", async () => {
    getEquityPositions.mockResolvedValue([{ symbol: "LLY", qty: 1, market_value: 10, unrealized_pl: 0 }]);
    const onSelect = vi.fn();
    await render(onSelect);
    await act(async () => {
      container.querySelector("tbody tr")!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    expect(onSelect).toHaveBeenCalledWith("LLY");
  });
});
