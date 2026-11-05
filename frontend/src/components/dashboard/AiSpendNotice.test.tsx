/**
 * The monthly AI spend line on the dashboard.
 *
 * @vitest-environment jsdom
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api, type LlmSpend } from "@/lib/api";
import { AiSpendNotice } from "./AiSpendNotice";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let host: HTMLElement | null = null;

async function mount(enabled = true) {
  host = document.createElement("div");
  document.body.appendChild(host);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  root = createRoot(host);
  await act(async () => {
    root!.render(
      <QueryClientProvider client={qc}>
        <AiSpendNotice enabled={enabled} />
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

const ok = (over: Record<string, unknown> = {}): LlmSpend => ({
  enabled: true, status: "ok", cap_usd: 30, reserve_usd: 0.2, counted_usd: 6.2, headroom_usd: 23.6,
  unpriced_responses: 0, allowed_paid_models: ["openrouter:deepseek/deepseek-v4.1-flash-1017"], ...over,
}) as LlmSpend;

describe("AiSpendNotice", () => {
  it("shows what is counted, the cap and what is left", async () => {
    vi.spyOn(api, "getLlmSpend").mockResolvedValue(ok());
    const el = await mount();
    const line = el.querySelector("[data-testid=ai-spend]")!;
    expect(line.textContent).toContain("$6.20 of $30.00");
    expect(line.textContent).toContain("$23.60 left");
    expect(line.classList.contains("is-low")).toBe(false);
  });

  it("turns to the attention colour when under a fifth of the cap is left", async () => {
    vi.spyOn(api, "getLlmSpend").mockResolvedValue(ok({ counted_usd: 25, headroom_usd: 4.5 }));
    const el = await mount();
    expect(el.querySelector("[data-testid=ai-spend]")!.classList.contains("is-low")).toBe(true);
  });

  it("says plainly when paid requests are paused, and that free models keep running", async () => {
    vi.spyOn(api, "getLlmSpend").mockResolvedValue({
      enabled: true, status: "paused", reason: "Spend baseline is unreadable; paid requests paused",
      cap_usd: 30, reserve_usd: 0.2, allowed_paid_models: [],
    });
    const el = await mount();
    const line = el.querySelector("[data-testid=ai-spend]")!;
    expect(line.textContent).toContain("Paid AI requests are paused");
    expect(line.textContent).toContain("Free models keep running");
    expect(line.classList.contains("is-low")).toBe(true);
  });

  it("mentions responses counted at the worst case", async () => {
    vi.spyOn(api, "getLlmSpend").mockResolvedValue(ok({ unpriced_responses: 2 }));
    const el = await mount();
    expect(el.textContent).toContain("2 responses counted at the worst case");
  });

  it("renders nothing without a cap, on a failed read, or when not enabled", async () => {
    vi.spyOn(api, "getLlmSpend").mockResolvedValue({ enabled: false });
    expect((await mount()).querySelector("[data-testid=ai-spend]")).toBeNull();
    act(() => root?.unmount()); host?.remove();
    vi.spyOn(api, "getLlmSpend").mockRejectedValue(new Error("403"));
    expect((await mount()).querySelector("[data-testid=ai-spend]")).toBeNull();
    act(() => root?.unmount()); host?.remove();
    const spy = vi.spyOn(api, "getLlmSpend").mockResolvedValue(ok());
    spy.mockClear();
    expect((await mount(false)).querySelector("[data-testid=ai-spend]")).toBeNull();
    expect(spy).not.toHaveBeenCalled();
  });
});
