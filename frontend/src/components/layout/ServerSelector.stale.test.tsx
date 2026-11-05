/**
 * A saved server name the backend no longer lists must not stick.
 *
 * The selection lives in localStorage, so after config.yml is reset (or a
 * server is deleted/renamed) the browser kept asking for the old name and every
 * page failed with "Server '<name>' not found" — including the Keys form, which
 * then could not save a credential. The selector now treats an unknown name as
 * no selection.
 *
 * @vitest-environment jsdom
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ServerContext } from "@/hooks/useServer";
import { ServerSelector } from "./ServerSelector";

declare global {
  var IS_REACT_ACT_ENVIRONMENT: boolean;
}

let container: HTMLDivElement;
let root: Root;
let serverList: unknown[] = [];

function srv(name: string, online = true, is_default = false) {
  return { name, host: "h", port: 8000, online, permission: "owner", is_default };
}

beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      new Response(JSON.stringify(serverList), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    ),
  );
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

async function renderWith(saved: string | null) {
  const setServer = vi.fn();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  await act(async () => {
    root.render(
      <QueryClientProvider client={client}>
        <ServerContext value={{ server: saved, setServer }}>
          <ServerSelector />
        </ServerContext>
      </QueryClientProvider>,
    );
  });
  // Let the servers query resolve and the effect run.
  await act(async () => {
    await new Promise((r) => setTimeout(r, 0));
  });
  return setServer;
}

describe("ServerSelector with a stale saved server", () => {
  it("replaces an unknown saved name with the default online server", async () => {
    serverList = [srv("other"), srv("main", true, true)];
    const setServer = await renderWith("local");
    expect(setServer).toHaveBeenCalledWith("main");
  });

  it("clears an unknown saved name when no server is online to replace it", async () => {
    serverList = [];
    const setServer = await renderWith("local");
    expect(setServer).toHaveBeenCalledWith("");
  });

  it("keeps a saved name the backend still lists, even while it is offline", async () => {
    serverList = [srv("local", false), srv("main", true, true)];
    const setServer = await renderWith("local");
    expect(setServer).not.toHaveBeenCalled();
  });
});
