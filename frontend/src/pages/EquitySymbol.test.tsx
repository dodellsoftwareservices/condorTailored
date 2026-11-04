/**
 * EquitySymbol: URL → queries → the chart/order-form props they feed.
 * EquityChart, EquityOrderForm and EquityAccountSummary each have their own
 * dedicated tests, so they're stubbed here to keep this one focused on the
 * page's own wiring — which symbol each query asks for, the period→interval
 * mapping, and the cost-basis figure derived from positions.
 *
 * @vitest-environment jsdom
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  EquityBarsResponse,
  EquityInstrument,
  EquityPosition,
  EquityQuote,
} from "@/lib/api";
import { EquitySymbol } from "./EquitySymbol";

const getEquityInstrument = vi.fn<(symbol: string) => Promise<EquityInstrument>>();
const getEquityQuote = vi.fn<(symbol: string) => Promise<EquityQuote>>();
const getEquityBars = vi.fn<(symbol: string, period: string, interval: string) => Promise<EquityBarsResponse>>();
const getEquityPositions = vi.fn<() => Promise<EquityPosition[]>>();

let lastChartProps: { bars: unknown; costBasis: unknown } | null = null;

vi.mock("@/lib/api", () => ({
  api: {
    getEquityInstrument: (s: string) => getEquityInstrument(s),
    getEquityQuote: (s: string) => getEquityQuote(s),
    getEquityBars: (s: string, p: string, i: string) => getEquityBars(s, p, i),
    getEquityPositions: () => getEquityPositions(),
  },
  errorStatus: (error: unknown) => (error as { status?: number } | null)?.status,
}));

vi.mock("@/components/equities/EquityAccountSummary", () => ({
  EquityAccountSummary: () => null,
}));
vi.mock("@/components/equities/EquityOrderForm", () => ({
  EquityOrderForm: ({ symbol }: { symbol: string }) => <div data-testid="order-form">{symbol}</div>,
}));
vi.mock("@/components/equities/EquityChart", () => ({
  EquityChart: (props: { bars: unknown; costBasis: unknown }) => {
    lastChartProps = props;
    return <div data-testid="chart" />;
  },
}));

declare global {
  // eslint-disable-next-line no-var
  var IS_REACT_ACT_ENVIRONMENT: boolean;
}

let container: HTMLDivElement;
let root: Root;

const INSTRUMENT: EquityInstrument = {
  symbol: "AAPL",
  name: "Apple Inc.",
  exchange: "NMS",
  currency: "USD",
  quote_type: "EQUITY",
  source: "yfinance",
};
const QUOTE: EquityQuote = {
  symbol: "AAPL",
  bid: 234.5,
  ask: 234.7,
  bid_size: 1,
  ask_size: 2,
  timestamp: 1_700_000_000,
  source: "alpaca",
};
const BARS: EquityBarsResponse = {
  symbol: "AAPL",
  interval: "1d",
  period: "3mo",
  source: "yfinance",
  bars: [{ timestamp: 1, open: 1, high: 1, low: 1, close: 1, volume: 1 }],
};

beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  lastChartProps = null;
  getEquityInstrument.mockReset().mockResolvedValue(INSTRUMENT);
  getEquityQuote.mockReset().mockResolvedValue(QUOTE);
  getEquityBars.mockReset().mockResolvedValue(BARS);
  getEquityPositions.mockReset().mockResolvedValue([]);
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

async function render(path = "/equities/AAPL") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  await act(async () => {
    root.render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route path="/equities/:symbol" element={<EquitySymbol />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
  });
}

/** Real macrotask ticks — a bare microtask flush is not enough for useQuery's
 * own scheduling to settle (HubBrowser.test.tsx's own pattern). */
async function waitFor(done: () => boolean) {
  for (let i = 0; i < 50 && !done(); i++) {
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
  }
  if (!done()) throw new Error("waitFor: condition never became true");
}

describe("EquitySymbol", () => {
  it("reads the symbol from the URL and asks every query for it", async () => {
    await render("/equities/aapl");
    await waitFor(() => container.textContent?.includes("Apple Inc.") ?? false);

    // The page uppercases what it passes to queries/components, though the
    // URL itself may carry whatever case the search page or a typed link used.
    expect(getEquityInstrument).toHaveBeenCalledWith("aapl");
    expect(getEquityQuote).toHaveBeenCalledWith("aapl");
    expect(getEquityBars).toHaveBeenCalledWith("aapl", "3mo", "1d");
    expect(container.textContent).toContain("AAPL"); // the header, uppercased
    expect(container.querySelector("[data-testid=order-form]")?.textContent).toBe("AAPL");
  });

  it("shows the bid/ask from Alpaca, labeled as polling", async () => {
    await render();
    await waitFor(() => container.textContent?.includes("$234.50") ?? false);

    expect(container.textContent).toContain("$234.50 / $234.70");
    expect(container.textContent).toContain("polling every");
    expect(container.textContent).not.toMatch(/\blive\b/i);
  });

  it("maps each period to its own interval", async () => {
    await render();
    await waitFor(() => getEquityBars.mock.calls.length > 0);
    expect(getEquityBars).toHaveBeenCalledWith("AAPL", "3mo", "1d");

    const fiveDay = [...container.querySelectorAll("button")].find(
      (b) => b.textContent === "5D",
    )!;
    act(() => fiveDay.click());
    await waitFor(() =>
      getEquityBars.mock.calls.some(([, p]) => p === "5d"),
    );

    expect(getEquityBars).toHaveBeenCalledWith("AAPL", "5d", "15m");
  });

  it("derives cost basis from market_value and unrealized_pl, not a raw field", async () => {
    getEquityPositions.mockResolvedValue([
      { symbol: "AAPL", qty: 2, market_value: 220, unrealized_pl: 20 },
    ]);
    await render();
    await waitFor(() => lastChartProps?.costBasis != null);

    // (220 - 20) / 2 = 100: what was paid per share, not the current price.
    expect(lastChartProps?.costBasis).toBe(100);
  });

  it("passes no cost basis when no position is held", async () => {
    await render();
    await waitFor(() => Array.isArray(lastChartProps?.bars) && (lastChartProps!.bars as unknown[]).length > 0);
    expect(lastChartProps?.costBasis).toBeNull();
  });

  it("shows a not-found notice instead of the page on a 404", async () => {
    getEquityInstrument.mockRejectedValue(Object.assign(new Error("not found"), { status: 404 }));
    await render();
    await waitFor(() => container.textContent?.includes("No instrument found") ?? false);

    expect(container.querySelector("[data-testid=order-form]")).toBeNull();
  });
});
