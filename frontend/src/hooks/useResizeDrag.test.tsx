/** @vitest-environment jsdom */
import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useResizeDrag } from "./useResizeDrag";

let container: HTMLDivElement;
let root: Root;
let frames: Map<number, FrameRequestCallback>;
let nextFrame: number;

function Harness({ max = 500 }: { max?: number }) {
  const [size, setSize] = useState(288);
  const { onMouseDown, onPointerDown, isDragging } = useResizeDrag({
    axis: "x", value: size, onChange: setSize, min: 200, max,
    direction: "inverted", cursor: "col-resize", lockUserSelect: true,
  });
  return <div id="seam" onMouseDown={onMouseDown} onPointerDown={onPointerDown} data-size={size} data-dragging={isDragging} />;
}

function pointer(target: EventTarget, type: string, x: number, id = 1, button = 0) {
  const event = new MouseEvent(type, { bubbles: true, cancelable: true, clientX: x, button });
  Object.defineProperties(event, { pointerId: { value: id }, isPrimary: { value: true } });
  act(() => target.dispatchEvent(event));
}
const seam = () => container.querySelector<HTMLElement>("#seam")!;
const size = () => Number(seam().dataset.size);

beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  frames = new Map();
  nextFrame = 0;
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => { frames.set(++nextFrame, callback); return nextFrame; });
  vi.stubGlobal("cancelAnimationFrame", (id: number) => frames.delete(id));
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  act(() => root.render(<Harness />));
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  document.body.style.cursor = "";
  document.body.style.userSelect = "";
  document.body.style.removeProperty("zoom");
  vi.unstubAllGlobals();
});

describe("shared pointer resizing", () => {
  it("keeps the resized edge under the pointer at 90% app scale", () => {
    document.body.style.zoom = "0.9";
    pointer(seam(), "pointerdown", 500);
    pointer(document, "pointermove", 410);
    pointer(document, "pointerup", 410);
    // 90 viewport pixels correspond to 100 pixels of CSS panel width.
    expect(size()).toBeCloseTo(388);
  });

  it("coalesces moves and commits the final position on release", () => {
    pointer(seam(), "pointerdown", 500);
    pointer(document, "pointermove", 480);
    pointer(document, "pointermove", 450);
    expect(size()).toBe(288);
    expect(frames.size).toBe(1);
    act(() => { const callback = [...frames.values()][0]; frames.clear(); callback(16); });
    expect(size()).toBe(338);
    pointer(document, "pointermove", 400);
    pointer(document, "pointerup", 400);
    expect(size()).toBe(388);
    expect(frames.size).toBe(0);
    expect(seam().dataset.dragging).toBe("false");
  });

  it("ignores other fingers and compatibility mouse events", () => {
    pointer(seam(), "pointerdown", 500, 7);
    act(() => seam().dispatchEvent(new MouseEvent("mousedown", { bubbles: true, clientX: 100 })));
    pointer(document, "pointermove", 200, 8);
    pointer(document, "pointerup", 200, 8);
    expect(seam().dataset.dragging).toBe("true");
    pointer(document, "pointermove", 450, 7);
    pointer(document, "pointerup", 450, 7);
    expect(size()).toBe(338);
  });

  it.each(["pointercancel", "blur"])("restores existing body styles after %s", (end) => {
    document.body.style.cursor = "crosshair";
    document.body.style.userSelect = "text";
    pointer(seam(), "pointerdown", 500);
    expect(document.body.style.cursor).toBe("col-resize");
    expect(document.body.style.userSelect).toBe("none");
    if (end === "blur") act(() => window.dispatchEvent(new Event("blur")));
    else pointer(document, end, 500);
    expect(document.body.style.cursor).toBe("crosshair");
    expect(document.body.style.userSelect).toBe("text");
    expect(seam().dataset.dragging).toBe("false");
  });

  it("cancels queued work and listeners on unmount", () => {
    pointer(seam(), "pointerdown", 500);
    pointer(document, "pointermove", 400);
    act(() => root.render(null));
    expect(frames.size).toBe(0);
    expect(document.body.style.cursor).toBe("");
    pointer(document, "pointermove", 300);
    expect(frames.size).toBe(0);
  });

  it("never starts a right-button drag", () => {
    pointer(seam(), "pointerdown", 500, 1, 2);
    expect(seam().dataset.dragging).toBe("false");
    expect(document.body.style.cursor).toBe("");
  });

  it("honors a viewport ceiling smaller than the usual minimum", () => {
    act(() => root.render(<Harness max={150} />));
    pointer(seam(), "pointerdown", 500);
    pointer(document, "pointermove", 400);
    pointer(document, "pointerup", 400);
    expect(size()).toBe(150);
  });
});
