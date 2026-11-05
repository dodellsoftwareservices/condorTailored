/** @vitest-environment jsdom */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ServerContext } from "@/hooks/useServer";
import { GatewaySettings } from "./GatewaySettings";

let root: Root;
let container: HTMLDivElement;
let qc: QueryClient;
let statusResponse: () => Promise<Response>;

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  statusResponse = async () => json({ running: false, info: null });
  vi.stubGlobal("fetch", vi.fn(async (path: string) => {
    if (path.includes("/gateway/status")) return statusResponse();
    if (path.endsWith("/api/v1/servers")) {
      return json([{ name: "local", host: "h", port: 8000, online: true, permission: "owner" }]);
    }
    return json({});
  }));
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  qc.clear();
  container.remove();
  vi.unstubAllGlobals();
});

async function flush() {
  for (let i = 0; i < 20; i++) {
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });
  }
}

async function mount() {
  await act(async () => {
    root.render(
      <QueryClientProvider client={qc}>
        <ServerContext.Provider value={{ server: "local", setServer: () => {} }}>
          <GatewaySettings />
        </ServerContext.Provider>
      </QueryClientProvider>,
    );
  });
  await flush();
}

function button(label: string) {
  return [...container.querySelectorAll("button")].find((b) => b.textContent?.trim() === label);
}

describe("Gateway status diagnostics", () => {
  it("reports an API failure as unavailable and retries to recover the running state", async () => {
    statusResponse = async () => json({ detail: "The trading API is unreachable" }, 502);
    await mount();

    expect(container.textContent).toContain("Gateway Status unavailable");
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("The trading API is unreachable");
    expect(container.textContent).not.toContain("Gateway Stopped");
    expect(button("Deploy")).toBeUndefined();
    expect(button("Restart")).toBeUndefined();
    expect(button("Stop")).toBeUndefined();

    statusResponse = async () => json({ running: true, info: {} });
    await act(async () => button("Retry status check")!.click());
    await flush();

    expect(container.textContent).toContain("Gateway Running");
    expect(container.querySelector('[role="alert"]')).toBeNull();
    expect(button("Restart")).toBeDefined();
    expect(button("Stop")).toBeDefined();
  });

  it("does not offer deployment before the first status response", async () => {
    let finish!: (response: Response) => void;
    statusResponse = () => new Promise((resolve) => { finish = resolve; });
    await mount();

    expect(container.textContent).toContain("Gateway ...");
    expect(button("Deploy")).toBeUndefined();

    await act(async () => finish(json({ running: false, info: null })));
    await flush();
    expect(container.textContent).toContain("Gateway Stopped");
    expect(button("Deploy")).toBeDefined();
  });

  it("does not show a cached running state when the next status poll fails", async () => {
    statusResponse = async () => json({ running: true, info: {} });
    await mount();
    expect(container.textContent).toContain("Gateway Running");

    statusResponse = async () => json({ detail: "Could not read Gateway status" }, 502);
    await act(async () => { await qc.refetchQueries({ queryKey: ["gateway-status", "local"] }); });
    await flush();

    expect(container.textContent).toContain("Gateway Status unavailable");
    expect(container.textContent).not.toContain("Gateway Running");
    expect(button("Restart")).toBeUndefined();
    expect(button("Deploy")).toBeUndefined();
  });
});
