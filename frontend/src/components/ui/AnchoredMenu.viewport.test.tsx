/** @vitest-environment jsdom */
import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AnchoredMenu } from "./AnchoredMenu";

let container: HTMLDivElement;
let root: Root;
let viewport: EventTarget & { width: number; height: number; offsetLeft: number; offsetTop: number };
let triggerRect: DOMRect;

function Harness({ match }: { match?: "min" | "exact" }) {
  const [anchor, setAnchor] = useState<HTMLButtonElement | null>(null);
  return <><button ref={setAnchor}>Choose</button><AnchoredMenu anchor={anchor} open onClose={() => {}} role="listbox" matchAnchorWidth={match}><button>Option</button></AnchoredMenu></>;
}
const menu = () => document.querySelector<HTMLElement>('[role="listbox"]')!;

beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  viewport = Object.assign(new EventTarget(), { width: 390, height: 800, offsetLeft: 0, offsetTop: 0 });
  vi.stubGlobal("visualViewport", viewport);
  vi.stubGlobal("innerHeight", 800);
  vi.stubGlobal("innerWidth", 390);
  triggerRect = { top: 300, bottom: 332, left: 350, right: 382, width: 32, height: 32 } as DOMRect;
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    return this.tagName === "BUTTON" ? triggerRect : { width: 260, height: 200 } as DOMRect;
  });
  container = document.createElement("div"); document.body.append(container); root = createRoot(container);
});
afterEach(() => { act(() => root.unmount()); container.remove(); document.body.style.removeProperty("zoom"); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("popover visual viewport", () => {
  it("keeps the menu attached to its trigger under app scaling", () => {
    document.body.style.zoom = "0.9";
    act(() => root.render(<Harness match="exact" />));
    expect(Number.parseFloat(menu().style.top) * 0.9).toBeCloseTo(triggerRect.bottom + 6 * 0.9);
    expect(Number.parseFloat(menu().style.width) * 0.9).toBeCloseTo(triggerRect.width);
    // Edge padding and placement use the same coordinate system as the menu.
    expect(Number.parseFloat(menu().style.left) * 0.9).toBeGreaterThanOrEqual(8 * 0.9);
  });

  it("shifts a wide menu inward instead of squeezing it at the right edge", () => {
    act(() => root.render(<Harness />));
    expect(menu().style.left).toBe("122px");
    expect(menu().style.maxWidth).toBe("374px");
  });

  it("moves above its trigger when a software keyboard takes the lower viewport", () => {
    act(() => root.render(<Harness />));
    viewport.height = 360;
    act(() => viewport.dispatchEvent(new Event("resize")));
    expect(menu().style.bottom).toBe("506px");
    expect(menu().style.maxHeight).toBe("286px");
  });

  it.each(["min", "exact"] as const)("caps %s anchor width so CSS min-width cannot push the menu offscreen", (match) => {
    triggerRect = { ...triggerRect, left: 0, right: 500, width: 500 } as DOMRect;
    act(() => root.render(<Harness match={match} />));
    expect(match === "min" ? menu().style.minWidth : menu().style.width).toBe("374px");
  });

  it("tracks a zoomed and offset visible area", () => {
    viewport.width = 280; viewport.offsetLeft = 40; viewport.offsetTop = 120;
    act(() => root.render(<Harness />));
    expect(Number.parseFloat(menu().style.left)).toBeGreaterThanOrEqual(48);
    expect(menu().style.maxWidth).toBe("264px");
  });
});
