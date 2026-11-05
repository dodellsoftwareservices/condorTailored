/**
 * The guide is `docs/get-started.md` rendered in a side panel, so what can break
 * is the rendering and the panel's own behaviour: a heading that loses its id
 * leaves the doc's "see Step 2" links pointing at nothing, a link that writes a
 * hash into the address bar leaves a back-button entry on whichever page is open
 * behind it, and a panel that cannot be closed is a panel in the way.
 *
 * @vitest-environment jsdom
 */

import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { GuidePanel } from "./GuidePanel";

declare global {
  // eslint-disable-next-line no-var
  var IS_REACT_ACT_ENVIRONMENT: boolean;
}

let container: HTMLDivElement;
let root: Root;
const onClose = vi.fn<() => void>();
const scrollIntoView = vi.fn<() => void>();

beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  // jsdom has no layout, so it has no scrollIntoView either.
  Element.prototype.scrollIntoView = scrollIntoView;
  scrollIntoView.mockClear();
  onClose.mockClear();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root.render(createElement(GuidePanel, { onClose })));
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe("GuidePanel", () => {
  it("renders the doc under its own title, in the compact reading styles", () => {
    expect(container.querySelector("h1")?.textContent).toBe("Get started");
    expect(container.querySelector(".docs-markdown--compact")).not.toBeNull();
    expect(container.querySelector(".chat-markdown")).toBeNull();
  });

  it("is a labelled complementary region", () => {
    const panel = container.querySelector("aside");
    expect(panel?.getAttribute("aria-label")).toBe("Get started guide");
    expect(panel?.id).toBe("guide-panel");
  });

  it("gives every heading an id, and no two the same", () => {
    const ids = [...container.querySelectorAll("h1, h2, h3")].map((h) => h.id);
    expect(ids.every(Boolean)).toBe(true);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("gives every in-guide link a heading to land on", () => {
    const hashes = [...container.querySelectorAll<HTMLAnchorElement>('a[href^="#"]')].map(
      (a) => a.getAttribute("href")!.slice(1),
    );
    // The doc sends the reader back to Steps 2, 3 and 4 from elsewhere.
    expect(hashes.length).toBeGreaterThanOrEqual(3);
    for (const id of hashes) {
      expect(container.querySelector(`[id="${id}"]`), `#${id}`).not.toBeNull();
    }
  });

  it("scrolls to the section on click and leaves the address bar alone", () => {
    const link = container.querySelector<HTMLAnchorElement>('a[href^="#"]')!;
    const before = window.location.href;
    const event = new MouseEvent("click", { bubbles: true, cancelable: true });
    act(() => {
      link.dispatchEvent(event);
    });
    expect(event.defaultPrevented).toBe(true);
    expect(scrollIntoView).toHaveBeenCalledTimes(1);
    expect(window.location.href).toBe(before);
  });

  it("closes from its button", () => {
    const close = container.querySelector<HTMLButtonElement>('button[aria-label="Close guide"]')!;
    act(() => {
      close.click();
    });
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
