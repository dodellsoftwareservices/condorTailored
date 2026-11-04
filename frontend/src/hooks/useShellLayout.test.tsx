/**
 * @vitest-environment jsdom
 */
import { act, createElement, useEffect } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { RAIL_COMPACT, RAIL_DEFAULT, RAIL_MAX, useShellLayout } from "./useShellLayout";

declare global { var IS_REACT_ACT_ENVIRONMENT: boolean; }
type Shell = ReturnType<typeof useShellLayout>;
let shell: Shell;
let container: HTMLDivElement;
let root: Root;

function Harness() {
  const current = useShellLayout();
  useEffect(() => { shell = current; });
  return <div data-testid="layout" style={current.style} />;
}
const layout = () => container.querySelector<HTMLElement>('[data-testid="layout"]')!;

beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  localStorage.clear();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root.render(createElement(Harness)));
});
afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe("useShellLayout", () => {
  it("collapses, expands, and remembers the desktop rail", () => {
    expect(layout().style.getPropertyValue("--shell-rail-width")).toBe(`${RAIL_DEFAULT}px`);
    act(() => shell.toggleCollapsed());
    expect(layout().style.getPropertyValue("--shell-rail-width")).toBe(`${RAIL_COMPACT}px`);
    expect(localStorage.getItem("condor_shell_rail_collapsed")).toBe("1");
    act(() => shell.toggleCollapsed());
    expect(localStorage.getItem("condor_shell_rail_collapsed")).toBe("0");
  });

  it("supports keyboard resizing with bounded, persistent widths", () => {
    const event = { key: "ArrowRight", preventDefault() {} } as React.KeyboardEvent<HTMLDivElement>;
    act(() => shell.onResizeKeyDown(event));
    expect(shell.railWidth).toBe(RAIL_DEFAULT + 12);
    for (let i = 0; i < 20; i++) act(() => shell.onResizeKeyDown(event));
    expect(shell.railWidth).toBe(RAIL_MAX);
    expect(localStorage.getItem("condor_shell_rail_width")).toBe(String(RAIL_MAX));
  });

  it("toggles focus independently from the stored navigation width", () => {
    act(() => shell.toggleFocusMode());
    expect(shell.focusMode).toBe(true);
    expect(shell.railWidth).toBe(RAIL_DEFAULT);
    expect(localStorage.getItem("condor_shell_focus_mode")).toBe("1");
  });
});
