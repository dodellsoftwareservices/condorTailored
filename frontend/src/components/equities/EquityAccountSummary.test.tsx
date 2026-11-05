/**
 * @vitest-environment jsdom
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { EquityAccount, EquityMarketStatus } from "@/lib/api";
import { EquityAccountSummary } from "./EquityAccountSummary";

const getEquityAccount = vi.fn<() => Promise<EquityAccount>>();
const getEquityMarketStatus = vi.fn<() => Promise<EquityMarketStatus>>();

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      getEquityAccount: () => getEquityAccount(),
      getEquityMarketStatus: () => getEquityMarketStatus(),
    },
  };
});

declare global {
  // eslint-disable-next-line no-var
  var IS_REACT_ACT_ENVIRONMENT: boolean;
}

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  getEquityAccount.mockReset();
  getEquityMarketStatus.mockReset();
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

async function render() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root.render(
      <QueryClientProvider client={client}>
        <EquityAccountSummary />
      </QueryClientProvider>,
    );
  });
}

// Real macrotask ticks, matching HubBrowser.test.tsx's own helper — a bare
// microtask flush is not enough for useQuery's own scheduling to settle.
async function waitFor(done: () => boolean) {
  for (let i = 0; i < 50 && !done(); i++) {
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
  }
  if (!done()) throw new Error("waitFor: condition never became true");
}

describe("EquityAccountSummary", () => {
  it("shows equity, cash and buying power once loaded", async () => {
    getEquityAccount.mockResolvedValue({ equity: 10_234.5, cash: 5_000, buying_power: 9_000 });
    getEquityMarketStatus.mockResolvedValue({ is_open: true, next_transition: "" });

    await render();
    await waitFor(() => container.textContent?.includes("$10,234.50") ?? false);

    expect(container.textContent).toContain("$5,000.00");
    expect(container.textContent).toContain("$9,000.00");
    expect(container.textContent).toContain("Market open");
  });

  it("shows market closed when the clock says so", async () => {
    getEquityAccount.mockResolvedValue({ equity: 1, cash: 1, buying_power: 1 });
    getEquityMarketStatus.mockResolvedValue({ is_open: false, next_transition: "" });

    await render();
    await waitFor(() => (container.textContent?.includes("Market closed") ?? false));

    expect(container.textContent).not.toContain("Market open");
  });

  it("shows a specific notice instead of a generic error on a 403", async () => {
    getEquityAccount.mockRejectedValue(Object.assign(new Error("Forbidden"), { status: 403 }));
    getEquityMarketStatus.mockRejectedValue(Object.assign(new Error("Forbidden"), { status: 403 }));

    await render();
    await waitFor(
      () => container.textContent?.includes("Admin access is required to view the paper account.") ?? false,
    );

    expect(container.textContent).not.toContain("Forbidden");
  });
});
