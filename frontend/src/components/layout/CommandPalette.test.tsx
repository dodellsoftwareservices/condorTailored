/**
 * @vitest-environment jsdom
 */
import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { NAV_ITEMS } from "@/lib/nav";
import { CommandPalette } from "./CommandPalette";

declare global { var IS_REACT_ACT_ENVIRONMENT: boolean; }

let container: HTMLDivElement;
let root: Root;
let onClose: ReturnType<typeof vi.fn<() => void>>;
let onNavigate: ReturnType<typeof vi.fn<(to: string) => void>>;
let onOpenGuide: ReturnType<typeof vi.fn<() => void>>;
let onReportIssue: ReturnType<typeof vi.fn<() => void>>;

const input = () => container.querySelector<HTMLInputElement>("input")!;
const options = () => [...container.querySelectorAll<HTMLButtonElement>('[role="option"]')];
const key = (value: string) => act(() => {
  container.querySelector("section")!.dispatchEvent(new KeyboardEvent("keydown", { key: value, bubbles: true }));
});

beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  onClose = vi.fn(); onNavigate = vi.fn(); onOpenGuide = vi.fn(); onReportIssue = vi.fn();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root.render(createElement(CommandPalette, {
    open: true, onClose, onNavigate, onOpenGuide, onReportIssue,
  })));
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe("CommandPalette", () => {
  it("keeps every main navigation destination reachable, including mobile-only overflow", () => {
    for (const item of NAV_ITEMS) {
      expect(options().some((option) => option.textContent?.includes(item.label))).toBe(true);
    }
  });

  it("filters commands and opens the selection with the keyboard", () => {
    const nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")!.set!;
    act(() => {
      nativeInputValueSetter.call(input(), "portfolio");
      input().dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(options()).toHaveLength(1);
    key("Enter");
    expect(onNavigate).toHaveBeenCalledWith("/portfolio");
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("exposes shell actions that are hidden in compact chrome", () => {
    const report = options().find((option) => option.textContent?.includes("Report an issue"))!;
    act(() => report.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    expect(onReportIssue).toHaveBeenCalledOnce();
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("closes with Escape without firing a command", () => {
    key("Escape");
    expect(onClose).toHaveBeenCalledOnce();
    expect(onNavigate).not.toHaveBeenCalled();
    expect(onOpenGuide).not.toHaveBeenCalled();
  });
});
