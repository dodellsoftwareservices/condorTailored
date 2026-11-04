import { useCallback, useEffect, useRef, useState } from "react";
import { getLayoutScale } from "@/lib/layout-scale";

type Axis = "x" | "y";

interface UseResizeDragOptions {
  /** Which pointer axis drives the resize. */
  axis: Axis;
  /** Current size, used as the drag's starting point. */
  value: number;
  /** Apply the clamped, computed size. */
  onChange: (next: number) => void;
  /** Minimum allowed size (clamp lower bound). Default 0. */
  min?: number;
  /**
   * Maximum allowed size (clamp upper bound). May be a function so it can be
   * recomputed each move (e.g. relative to `window.innerWidth`). Default Infinity.
   */
  max?: number | (() => number);
  /**
   * Sign of the delta. With "normal", moving the pointer in the positive axis
   * direction grows the size; with "inverted" it shrinks it (panels anchored to
   * the right/bottom). Default "normal".
   */
  direction?: "normal" | "inverted";
  /**
   * Override the size computation entirely. Receives the pointer coordinate on
   * `axis` in unscaled layout pixels and the size captured at drag start, and returns
   * the raw (pre-clamp) next size. Useful for absolute positioning (e.g.
   * `window.innerWidth - clientX`). When provided, `direction` is ignored.
   */
  compute?: (coord: number, startValue: number) => number;
  /** Cursor applied to `document.body` while dragging (e.g. "col-resize"). */
  cursor?: string;
  /** Disable text selection on `document.body` while dragging. Default false. */
  lockUserSelect?: boolean;
}

/**
 * Pointer resize for mouse, pen, and touch. Moves are coalesced to one update
 * per animation frame, and release always commits the last observed size.
 * Cancellation, window blur, and unmount restore the previous body styles.
 *
 * Use onPointerDown with touch-action:none on the handle. onMouseDown remains
 * available for legacy consumers; a compatibility mouse event cannot start a
 * second drag while a pointer gesture is active.
 */
export function useResizeDrag(options: UseResizeDragOptions) {
  const optsRef = useRef(options);
  useEffect(() => {
    optsRef.current = options;
  });

  const [isDragging, setIsDragging] = useState(false);
  // Holds the teardown for an in-flight drag so an unmount mid-drag still
  // detaches listeners and restores body cursor/userSelect.
  const dragCleanup = useRef<(() => void) | null>(null);

  const beginDrag = useCallback((e: React.MouseEvent | React.PointerEvent, pointer: boolean) => {
    if (e.button !== 0 || dragCleanup.current) return;
    if (pointer && (e as React.PointerEvent).isPrimary === false) return;
    e.preventDefault();
    const { axis, value, onChange, min = 0, max = Infinity, direction = "normal", compute, cursor, lockUserSelect } =
      optsRef.current;

    const scale = getLayoutScale();
    const startCoord = (axis === "x" ? e.clientX : e.clientY) / scale;
    const startValue = value;
    const pointerId = pointer ? (e as React.PointerEvent).pointerId : undefined;
    const previousCursor = document.body.style.cursor;
    const previousSelect = document.body.style.userSelect;
    let frame: number | null = null;
    let pending: number | null = null;

    setIsDragging(true);
    if (cursor) document.body.style.cursor = cursor;
    if (lockUserSelect) document.body.style.userSelect = "none";

    const flush = () => {
      frame = null;
      if (pending !== null) {
        onChange(pending);
        pending = null;
      }
    };
    const belongsToDrag = (ev: Event) => !pointer || pointerId === undefined || (ev as PointerEvent).pointerId === pointerId;
    const onMove = (event: Event) => {
      if (!belongsToDrag(event)) return;
      const ev = event as MouseEvent;
      const coord = (axis === "x" ? ev.clientX : ev.clientY) / scale;
      const raw = compute
        ? compute(coord, startValue)
        : direction === "inverted"
          ? startValue + (startCoord - coord)
          : startValue + (coord - startCoord);
      const upper = Math.max(0, typeof max === "function" ? max() : max);
      if (!Number.isFinite(raw) || Number.isNaN(upper)) return;
      pending = Math.max(Math.min(min, upper), Math.min(upper, raw));
      // Preserve synchronous mouse-only consumers; browser pointer gestures
      // take the coalesced path and suppress their compatibility mouse event.
      if (!pointer) { flush(); return; }
      if (frame === null) {
        if (typeof requestAnimationFrame === "function") frame = requestAnimationFrame(flush);
        else flush();
      }
    };

    const moveEvent = pointer ? "pointermove" : "mousemove";
    const upEvent = pointer ? "pointerup" : "mouseup";
    const cleanup = (commit: boolean) => {
      document.removeEventListener(moveEvent, onMove);
      document.removeEventListener(upEvent, onEnd);
      document.removeEventListener("pointercancel", onEnd);
      window.removeEventListener("blur", onBlur);
      if (frame !== null) cancelAnimationFrame(frame);
      if (commit) flush();
      if (cursor) document.body.style.cursor = previousCursor;
      if (lockUserSelect) document.body.style.userSelect = previousSelect;
      dragCleanup.current = null;
      setIsDragging(false);
    };
    const onEnd = (event: Event) => {
      if (belongsToDrag(event)) cleanup(true);
    };
    const onBlur = () => cleanup(true);

    dragCleanup.current = () => cleanup(false);
    document.addEventListener(moveEvent, onMove);
    document.addEventListener(upEvent, onEnd);
    if (pointer) document.addEventListener("pointercancel", onEnd);
    window.addEventListener("blur", onBlur);
  }, []);

  const onMouseDown = useCallback((event: React.MouseEvent) => beginDrag(event, false), [beginDrag]);
  const onPointerDown = useCallback((event: React.PointerEvent) => beginDrag(event, true), [beginDrag]);

  // Tear down a drag interrupted by unmount.
  useEffect(() => () => dragCleanup.current?.(), []);

  return { onMouseDown, onPointerDown, isDragging };
}
