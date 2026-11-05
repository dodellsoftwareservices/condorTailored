/** @vitest-environment jsdom */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ConfirmDialog } from "./ConfirmDialog";

let container: HTMLDivElement;
let root: Root;
const close = vi.fn();
const confirm = vi.fn();
const nativeOpen = vi.fn(function (this: HTMLDialogElement) { this.setAttribute("open", ""); });
const nativeClose = vi.fn(function (this: HTMLDialogElement) { this.removeAttribute("open"); });
let originalOpen: PropertyDescriptor | undefined;
let originalClose: PropertyDescriptor | undefined;

function render(props: Partial<Parameters<typeof ConfirmDialog>[0]> = {}) {
  act(() => root.render(<ConfirmDialog open title="Delete strategy?" onClose={close} onConfirm={confirm} {...props}>Saved runs remain available.</ConfirmDialog>));
}
beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  vi.clearAllMocks();
  originalOpen = Object.getOwnPropertyDescriptor(HTMLDialogElement.prototype, "showModal");
  originalClose = Object.getOwnPropertyDescriptor(HTMLDialogElement.prototype, "close");
  Object.defineProperty(HTMLDialogElement.prototype, "showModal", { configurable: true, value: nativeOpen });
  Object.defineProperty(HTMLDialogElement.prototype, "close", { configurable: true, value: nativeClose });
  container = document.createElement("div"); document.body.append(container); root = createRoot(container);
});
afterEach(() => {
  act(() => root.unmount()); container.remove();
  for (const [key, original] of [["showModal", originalOpen], ["close", originalClose]] as const) {
    if (original) Object.defineProperty(HTMLDialogElement.prototype, key, original);
    else Reflect.deleteProperty(HTMLDialogElement.prototype, key);
  }
});

describe("native confirmation modal", () => {
  it("opens in the top layer with a named title and description", () => {
    render();
    const dialog = document.querySelector("dialog")!;
    expect(nativeOpen).toHaveBeenCalledOnce();
    expect(container.contains(dialog)).toBe(false);
    expect(document.getElementById(dialog.getAttribute("aria-labelledby")!)?.textContent).toBe("Delete strategy?");
    expect(document.getElementById(dialog.getAttribute("aria-describedby")!)?.textContent).toBe("Saved runs remain available.");
    render({ open: false });
    expect(nativeClose).toHaveBeenCalledOnce();
    expect(document.querySelector("dialog")).toBeNull();
  });

  it("closes on native cancellation without closing a drawer underneath", () => {
    render();
    const dialog = document.querySelector("dialog")!;
    const parentEscape = vi.fn();
    window.addEventListener("keydown", parentEscape);
    act(() => dialog.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    expect(parentEscape).not.toHaveBeenCalled();
    window.removeEventListener("keydown", parentEscape);
    const cancel = new Event("cancel", { cancelable: true });
    act(() => dialog.dispatchEvent(cancel));
    expect(cancel.defaultPrevented).toBe(true);
    expect(close).toHaveBeenCalledOnce();
  });

  it("distinguishes clicking inside the card from the backdrop", () => {
    render();
    act(() => document.querySelector("h2")!.click());
    expect(close).not.toHaveBeenCalled();
    act(() => document.querySelector("dialog")!.click());
    expect(close).toHaveBeenCalledOnce();
  });

  it("keeps destructive actions disabled while pending and announces errors", () => {
    render({ isPending: true, isError: true, errorText: "Unable to delete" });
    const button = document.querySelector<HTMLButtonElement>('[aria-busy="true"]')!;
    expect(button.disabled).toBe(true);
    act(() => button.click());
    expect(confirm).not.toHaveBeenCalled();
    expect(document.querySelector('[role="alert"]')?.textContent).toBe("Unable to delete");
  });
});
