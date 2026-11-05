/**
 * @vitest-environment jsdom
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { EquityOrderResult, EquityRolloutStatus } from "@/lib/api";
import { EquityOrderForm } from "./EquityOrderForm";

const submitEquityOrder = vi.fn<() => Promise<EquityOrderResult>>();
const getEquityRolloutStatus = vi.fn<() => Promise<EquityRolloutStatus>>();

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      submitEquityOrder: (...args: unknown[]) =>
        (submitEquityOrder as unknown as (...a: unknown[]) => unknown)(...args),
      getEquityRolloutStatus: () => getEquityRolloutStatus(),
    },
  };
});

declare global {
  // eslint-disable-next-line no-var
  var IS_REACT_ACT_ENVIRONMENT: boolean;
}

let container: HTMLDivElement;
let root: Root;
let client: QueryClient;

const ENABLED: EquityRolloutStatus = {
  markets_enabled: true,
  paper_submissions_enabled: true,
};

beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  submitEquityOrder.mockReset();
  getEquityRolloutStatus.mockReset().mockResolvedValue(ENABLED);
  client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

async function render() {
  await act(async () => {
    root.render(
      <QueryClientProvider client={client}>
        <EquityOrderForm symbol="AAPL" />
      </QueryClientProvider>,
    );
  });
}

/**
 * Poll on a real macrotask tick, not a bare microtask flush — matches
 * HubBrowser.test.tsx's own helper. React Query's internal scheduling (even
 * for an already-resolved mock promise) settles a tick beyond what a plain
 * `await act(async () => {})` flushes, which an earlier version of this file
 * learned by having a test fail only when run after others, never alone.
 */
async function waitFor(done: () => boolean) {
  for (let i = 0; i < 50 && !done(); i++) {
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
  }
  if (!done()) throw new Error("waitFor: condition never became true");
}

function submitButton(): HTMLButtonElement {
  // Distinct from the side-toggle buttons (bare "buy"/"sell"): the real
  // submit button always names the symbol, or reads "Submitting…".
  return [...container.querySelectorAll("button")].find((b) =>
    /AAPL|Submitting/.test(b.textContent ?? ""),
  ) as HTMLButtonElement;
}

function sideButton(side: "buy" | "sell"): HTMLButtonElement {
  return [...container.querySelectorAll("button")].find(
    (b) => b.textContent?.trim().toLowerCase() === side,
  ) as HTMLButtonElement;
}

async function clickSubmit() {
  await act(async () => {
    submitButton().click();
  });
}

describe("EquityOrderForm", () => {
  it("submits the chosen side and amount for the given symbol", async () => {
    submitEquityOrder.mockResolvedValue({
      id: "o1",
      symbol: "AAPL",
      side: "buy",
      notional: 100,
      status: "accepted",
      submitted_at: null,
    });
    await render();
    await waitFor(() => submitButton().textContent === "Buy AAPL");

    await clickSubmit();
    await waitFor(() => container.textContent?.includes("Order accepted.") ?? false);

    expect(submitEquityOrder).toHaveBeenCalledWith({
      symbol: "AAPL",
      notional: 100,
      side: "buy",
    });
  });

  it("switches side and sends it on submit", async () => {
    submitEquityOrder.mockResolvedValue({
      id: "o1",
      symbol: "AAPL",
      side: "sell",
      notional: 100,
      status: "accepted",
      submitted_at: null,
    });
    await render();
    await waitFor(() => submitButton().textContent === "Buy AAPL");

    act(() => sideButton("sell").click());
    expect(submitButton().textContent).toBe("Sell AAPL");

    await clickSubmit();
    await waitFor(() => submitEquityOrder.mock.calls.length > 0);

    expect(submitEquityOrder).toHaveBeenCalledWith(
      expect.objectContaining({ side: "sell" }),
    );
  });

  it("invalidates positions and account after a successful submission", async () => {
    submitEquityOrder.mockResolvedValue({
      id: "o1",
      symbol: "AAPL",
      side: "buy",
      notional: 100,
      status: "accepted",
      submitted_at: null,
    });
    const invalidate = vi.spyOn(client, "invalidateQueries");
    await render();
    await waitFor(() => submitButton().textContent === "Buy AAPL");

    await clickSubmit();
    await waitFor(() => container.textContent?.includes("Order accepted.") ?? false);

    const keys = invalidate.mock.calls.map((c) => (c[0] as { queryKey: unknown[] }).queryKey);
    expect(keys).toContainEqual(["equity-positions"]);
    expect(keys).toContainEqual(["equity-account"]);
  });

  it("shows a distinct, non-retry message for an ambiguous submission", async () => {
    submitEquityOrder.mockRejectedValue(
      Object.assign(new Error("Order may have been submitted"), {
        status: 502,
        code: "equity_order_ambiguous",
      }),
    );
    await render();
    await waitFor(() => submitButton().textContent === "Buy AAPL");

    await clickSubmit();
    await waitFor(() => container.textContent?.includes("Do not retry blindly") ?? false);

    expect(container.textContent).toContain("Do not retry blindly");
  });

  it("shows an admin-access message on a 403, not the raw error", async () => {
    submitEquityOrder.mockRejectedValue(
      Object.assign(new Error("Admin access required"), { status: 403 }),
    );
    await render();
    await waitFor(() => submitButton().textContent === "Buy AAPL");

    await clickSubmit();
    await waitFor(
      () => container.textContent?.includes("Admin access is required to submit paper orders.") ?? false,
    );
  });

  it("shows the backend's own message for any other rejection", async () => {
    submitEquityOrder.mockRejectedValue(
      Object.assign(new Error("$15000.00 exceeds the $10000.00 per-order limit"), {
        status: 400,
        code: "equity_order_too_large",
      }),
    );
    await render();
    await waitFor(() => submitButton().textContent === "Buy AAPL");

    await clickSubmit();
    await waitFor(
      () => container.textContent?.includes("exceeds the $10000.00 per-order limit") ?? false,
    );
  });

  it("disables submission when paper trading is not enabled", async () => {
    getEquityRolloutStatus.mockResolvedValue({
      markets_enabled: true,
      paper_submissions_enabled: false,
    });
    await render();
    await waitFor(
      () => container.textContent?.includes("Paper trading is not enabled on this install.") ?? false,
    );

    expect(submitButton().disabled).toBe(true);
  });
});
