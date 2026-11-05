/**
 * Equities' search → navigate flow, mirroring Dex.paging.test.tsx's harness.
 *
 * @vitest-environment jsdom
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { EquitySearchResponse } from "@/lib/api";
import { Equities } from "./Equities";

const SEARCH_DEBOUNCE_MS = 400; // matches the constant inside Equities.tsx

const searchEquities = vi.fn<() => Promise<EquitySearchResponse>>();
const navigate = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    searchEquities: (...args: unknown[]) =>
      (searchEquities as unknown as (...a: unknown[]) => unknown)(...args),
  },
  errorStatus: () => undefined,
}));

vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return { ...actual, useNavigate: () => navigate };
});

vi.mock("@/components/equities/EquityAccountSummary", () => ({
  EquityAccountSummary: () => null, // covered by its own dedicated tests
}));

declare global {
  // eslint-disable-next-line no-var
  var IS_REACT_ACT_ENVIRONMENT: boolean;
}

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  searchEquities.mockReset();
  navigate.mockReset();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

async function render() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  await act(async () => {
    root.render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <Equities />
        </MemoryRouter>
      </QueryClientProvider>,
    );
  });
}

/**
 * Wait `ms` of real time, then one more flush — mirrors Dex.paging.test.tsx's
 * own `settle()` exactly, for the same 400ms debounce. A generic polling
 * `waitFor` (HubBrowser.test.tsx's pattern, used elsewhere in this change)
 * does not fit here: fifty `setTimeout(0)` ticks resolve far faster in real
 * wall-clock time than the 400ms the debounce's own real timer needs, so it
 * times out waiting on a condition nothing has driven forward yet.
 */
async function settle(ms = 0) {
  await act(() => new Promise((r) => setTimeout(r, ms)));
  await act(() => new Promise((r) => setTimeout(r, 0)));
}

/** Mirrors Dex.paging.test.tsx's own `type()` — set the final value once and
 * dispatch one input event, rather than simulate keystroke by keystroke. */
async function type(value: string) {
  const input = container.querySelector("input") as HTMLInputElement;
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
  await act(async () => {
    setter.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

describe("Equities", () => {
  it("fires exactly one search after the debounce, not before", async () => {
    searchEquities.mockResolvedValue({ query: "AAPL", results: [], source: "yfinance" });
    await render();

    await type("AAPL");
    expect(searchEquities).not.toHaveBeenCalled(); // debounce hasn't elapsed yet

    await settle(SEARCH_DEBOUNCE_MS);
    expect(searchEquities).toHaveBeenCalledTimes(1);
    // One argument: the page relies on searchEquities' own default limit=8
    // rather than passing it explicitly, so the mock never sees a second arg.
    expect(searchEquities).toHaveBeenCalledWith("AAPL");
  });

  it("navigates to the symbol page on a result click", async () => {
    searchEquities.mockResolvedValue({
      query: "apple",
      results: [{ symbol: "AAPL", name: "Apple Inc.", exchange: "NMS", quote_type: "EQUITY" }],
      source: "yfinance",
    });
    await render();

    await type("apple");
    await settle(SEARCH_DEBOUNCE_MS);

    const row = [...container.querySelectorAll("button")].find((b) =>
      b.textContent?.includes("AAPL"),
    )!;
    act(() => row.click());

    expect(navigate).toHaveBeenCalledWith("/equities/AAPL");
  });

  it("shows a no-matches message for an empty result set", async () => {
    searchEquities.mockResolvedValue({ query: "zzz", results: [], source: "yfinance" });
    await render();

    await type("zzz");
    await settle(SEARCH_DEBOUNCE_MS);

    expect(container.textContent).toContain('No matches for "zzz".');
  });

  it("does not search at all for a blank query", async () => {
    await render();
    // Past the debounce window: debouncedQuery resolves to "" either way,
    // but `enabled: debouncedQuery.length > 0` must keep the query off.
    await settle(SEARCH_DEBOUNCE_MS);
    expect(searchEquities).not.toHaveBeenCalled();
  });
});
