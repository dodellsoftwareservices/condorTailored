import { useCallback, useEffect, useRef, useState } from "react";
import type { CSSProperties, KeyboardEvent as ReactKeyboardEvent } from "react";
import { getLayoutScale } from "@/lib/layout-scale";

import {
  SHELL_RAIL_WIDTH_KEY as WIDTH_KEY,
  SHELL_RAIL_COLLAPSED_KEY as COLLAPSED_KEY,
  SHELL_FOCUS_MODE_KEY as FOCUS_KEY,
} from "@/lib/sessionState";

export const RAIL_MIN = 200;
export const RAIL_MAX = 288;
export const RAIL_DEFAULT = 232;
export const RAIL_COMPACT = 76;

function storedNumber(key: string, fallback: number) {
  try {
    const value = Number(localStorage.getItem(key));
    return Number.isFinite(value) && value >= RAIL_MIN && value <= RAIL_MAX
      ? value
      : fallback;
  } catch {
    return fallback;
  }
}

function storedBoolean(key: string) {
  try {
    return localStorage.getItem(key) === "1";
  } catch {
    return false;
  }
}

function persist(key: string, value: string | number | boolean) {
  try {
    localStorage.setItem(key, typeof value === "boolean" ? (value ? "1" : "0") : String(value));
  } catch {
    // The layout remains fully usable when storage is unavailable.
  }
}

/** Persistent, pointer/keyboard-resizable desktop navigation. CSS owns breakpoints. */
export function useShellLayout() {
  const [railWidth, setRailWidth] = useState(() => storedNumber(WIDTH_KEY, RAIL_DEFAULT));
  const [collapsed, setCollapsed] = useState(() => storedBoolean(COLLAPSED_KEY));
  const [focusMode, setFocusMode] = useState(() => storedBoolean(FOCUS_KEY));
  const [resizing, setResizing] = useState(false);
  const cleanupRef = useRef<(() => void) | null>(null);

  useEffect(() => persist(WIDTH_KEY, railWidth), [railWidth]);
  useEffect(() => persist(COLLAPSED_KEY, collapsed), [collapsed]);
  useEffect(() => persist(FOCUS_KEY, focusMode), [focusMode]);

  const resize = useCallback((next: number) => {
    setRailWidth(Math.max(RAIL_MIN, Math.min(RAIL_MAX, next)));
    if (next < RAIL_MIN - 24) setCollapsed(true);
  }, []);

  const onResizeStart = useCallback((event: React.PointerEvent) => {
    if (event.button !== 0) return;
    event.preventDefault();
    const scale = getLayoutScale();
    const startX = event.clientX / scale;
    const startWidth = railWidth;
    setCollapsed(false);
    setResizing(true);
    const previousCursor = document.body.style.cursor;
    const previousSelect = document.body.style.userSelect;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";

    const onMove = (move: PointerEvent) => {
      resize(startWidth + move.clientX / scale - startX);
    };
    const cleanup = () => {
      document.removeEventListener("pointermove", onMove);
      document.removeEventListener("pointerup", cleanup);
      document.removeEventListener("pointercancel", cleanup);
      document.body.style.cursor = previousCursor;
      document.body.style.userSelect = previousSelect;
      cleanupRef.current = null;
      setResizing(false);
    };
    cleanupRef.current = cleanup;
    document.addEventListener("pointermove", onMove);
    document.addEventListener("pointerup", cleanup);
    document.addEventListener("pointercancel", cleanup);
  }, [railWidth, resize]);

  useEffect(() => () => cleanupRef.current?.(), []);

  const onResizeKeyDown = useCallback((event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      event.preventDefault();
      setCollapsed(false);
      resize(railWidth + (event.key === "ArrowRight" ? 12 : -12));
    } else if (event.key === "Home") {
      event.preventDefault();
      setCollapsed(false);
      resize(RAIL_MIN);
    } else if (event.key === "End") {
      event.preventDefault();
      setCollapsed(false);
      resize(RAIL_MAX);
    } else if (event.key === "Enter") {
      event.preventDefault();
      setCollapsed((value) => !value);
    }
  }, [railWidth, resize]);

  const toggleCollapsed = useCallback(() => {
    setCollapsed((value) => !value);
    setFocusMode(false);
  }, []);
  const toggleFocusMode = useCallback(() => setFocusMode((value) => !value), []);

  const style = {
    "--shell-rail-width": `${collapsed ? RAIL_COMPACT : railWidth}px`,
  } as CSSProperties;

  return {
    railWidth,
    collapsed,
    focusMode,
    resizing,
    style,
    toggleCollapsed,
    toggleFocusMode,
    onResizeStart,
    onResizeKeyDown,
  };
}
