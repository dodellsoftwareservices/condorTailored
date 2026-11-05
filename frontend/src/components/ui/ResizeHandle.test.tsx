/** @vitest-environment jsdom */
import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it } from "vitest";

import { ResizeHandle } from "./ResizeHandle";

let container: HTMLDivElement;
let root: Root;

function Harness({ axis }: { axis: "x" | "y" }) {
  const [value, setValue] = useState(288);
  return <ResizeHandle axis={axis} value={value} onChange={setValue} min={260} max={500} reset={288} label="Resize order panel" />;
}
const handle = () => container.querySelector<HTMLElement>('[role="separator"]')!;
const value = () => Number(handle().getAttribute("aria-valuenow"));
function press(key: string, shiftKey = false) {
  act(() => handle().dispatchEvent(new KeyboardEvent("keydown", { key, shiftKey, bubbles: true, cancelable: true })));
}
beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  act(() => root.render(<Harness axis="x" />));
});
afterEach(() => { act(() => root.unmount()); container.remove(); });

it("supports arrow steps, accelerated steps, bounds, and resetting", () => {
  press("ArrowLeft"); expect(value()).toBe(304);
  press("ArrowLeft", true); expect(value()).toBe(352);
  press("Home"); expect(value()).toBe(260);
  press("ArrowRight"); expect(value()).toBe(260);
  press("End"); expect(value()).toBe(500);
  press("ArrowLeft"); expect(value()).toBe(500);
  press("Enter"); expect(value()).toBe(288);
  expect(handle().getAttribute("aria-orientation")).toBe("vertical");
  expect(handle().tabIndex).toBe(0);
});

it("grows the lower panel when the horizontal seam moves upward", () => {
  act(() => root.render(<Harness axis="y" />));
  press("ArrowUp"); expect(value()).toBe(304);
  press("ArrowDown"); expect(value()).toBe(288);
  expect(handle().getAttribute("aria-orientation")).toBe("horizontal");
});
