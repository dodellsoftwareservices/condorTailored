/** @vitest-environment jsdom */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ServerContext } from "@/hooks/useServer";
import { AuthContext } from "@/lib/auth";
import type { DashboardAccount } from "@/lib/api";
import { Dashboard } from "./Dashboard";

vi.mock("recharts", () => {
  const Wrapper = ({ children }: { children?: ReactNode }) => <div>{children}</div>;
  const Empty = () => null;
  return { ResponsiveContainer: Wrapper, AreaChart: Wrapper, Area: Empty, CartesianGrid: Empty, ReferenceLine: Empty, Tooltip: Empty, XAxis: Empty, YAxis: Empty };
});

const account = (id: string, label: string): DashboardAccount => ({ id, label, status: "online", starting_capital: 100000, equity: 101000, cash: 98500, deployed: 2500, pnl: 1000, roi_pct: 1, order_cap: 2500, volume: 5000, fees: 10, batting_average: 50, closed_trades: 2, wins: 1, losses: 1, breakeven: 0, profit_factor: 2, sharpe: null, daily_observations: 0, max_drawdown_pct: 0, outcomes_complete: true, points: [], notes: [] });
let container: HTMLDivElement;
let root: Root;
let qc: QueryClient;
let requests: { path: string; method: string }[];
let failed: boolean;

beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  requests = [];
  failed = false;
  vi.stubGlobal("fetch", vi.fn(async (path: string, init?: RequestInit) => {
    requests.push({ path, method: init?.method ?? "GET" });
    if (failed) return new Response(JSON.stringify({ detail: "Account provider unavailable" }), { status: 502 });
    const accounts = [account("alpaca", "Alpaca Paper"), account("kraken_sim", "KrakenSim")];
    const summary = { ...account("all", "All paper accounts"), starting_capital: 200000, equity: 202000, pnl: 2000, cash: 197000, deployed: 5000 };
    let body: unknown = {};
    if (path.includes("/dashboard/paper")) body = { as_of: 1800000000, range: "1M", accounts, summary };
    else if (path.includes("/portfolio/history")) body = { points: [] };
    else if (path.includes("/portfolio")) body = { total_usd: 1234, connectors: [] };
    return new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
  }));
});

afterEach(() => { act(() => root.unmount()); qc.clear(); container.remove(); vi.unstubAllGlobals(); });

async function flush() { for (let i = 0; i < 20; i++) await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); }); }
async function mount(url = "/") {
  await act(async () => { root.render(<QueryClientProvider client={qc}><AuthContext.Provider value={{ user: { id: 1, role: "admin", username: "owner", first_name: "Owner" }, token: "test-session", isAuthenticated: true, loginLocal: async () => {}, loginWithToken: async () => {}, logout: () => {} }}><ServerContext.Provider value={{ server: "local", setServer: () => {} }}><MemoryRouter initialEntries={[url]}><Dashboard /></MemoryRouter></ServerContext.Provider></AuthContext.Provider></QueryClientProvider>); });
  await flush();
}
const value = (label: string) => container.querySelector(`section[aria-label="${label}"] .dashboard-metric-value`)?.textContent;
const button = (label: string) => [...container.querySelectorAll("button")].find((b) => b.textContent?.trim() === label)!;

describe("portfolio dashboard", () => {
  it("shows both 100k starting accounts while preserving each 2500 order cap", async () => {
    await mount();
    expect(value("Total invested")).toBe("$200,000.00");
    expect(value("Portfolio value")).toBe("$202,000.00");
    expect(value("Net PnL")).toBe("+$2,000.00");
    expect(value("Sharpe ratio")).toBe("—");
    expect(container.querySelectorAll(".dashboard-limit")).toHaveLength(2);
    expect([...container.querySelectorAll(".dashboard-limit")].every((node) => node.textContent === "$2,500.00")).toBe(true);
    expect(requests.every((r) => r.method === "GET")).toBe(true);
  });

  it("filters the overview to one account without adding the other balance", async () => {
    await mount("/?account=kraken_sim");
    expect(value("Total invested")).toBe("$100,000.00");
    expect(value("Portfolio value")).toBe("$101,000.00");
  });

  it("keeps live and paper balances separate", async () => {
    await mount();
    await act(async () => button("Live exchanges").click());
    await flush();
    expect(value("Portfolio value")).toBe("$1,234.00");
    expect(value("Total invested")).toBe("—");
    expect(value("Return on investment")).toBe("—");
  });

  it("passes the selected range to the API", async () => {
    await mount();
    await act(async () => button("1W").click());
    await flush();
    expect(requests.some((r) => r.path.endsWith("range=1W"))).toBe(true);
  });

  it("reports errors without presenting missing account values as zero", async () => {
    failed = true;
    await mount();
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("Account provider unavailable");
    expect(value("Portfolio value")).toBe("—");
    expect(value("Net PnL")).toBe("—");
  });
});
