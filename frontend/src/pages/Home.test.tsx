/** @vitest-environment jsdom */
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Home } from "./Home";

vi.mock("./Dashboard", () => ({ Dashboard: () => <div>Portfolio dashboard</div> }));

function Chat() { const location = useLocation(); return <div>Agent workspace: {location.pathname}{location.search}</div>; }
let teardown = () => {};
afterEach(() => teardown());

async function render(url: string) {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const container = document.createElement("div");
  const root = createRoot(container);
  teardown = () => act(() => root.unmount());
  await act(async () => { root.render(<MemoryRouter initialEntries={[url]}><Routes><Route path="/" element={<Home />} /><Route path="/agents" element={<Chat />} /></Routes></MemoryRouter>); });
  return container.textContent;
}

describe("dashboard home routing", () => {
  it("opens the portfolio dashboard at the root", async () => { expect(await render("/")).toBe("Portfolio dashboard"); });
  it.each(["?agent=brigado&ask=status", "?conversation=abc", "?panel=desk&desk=execution"])("preserves bookmarked conversation parameters %s", async (query) => { expect(await render(`/${query}`)).toBe(`Agent workspace: /agents${query}`); });
});
