import type { CSSProperties } from "react";

import { useResizeDrag } from "@/hooks/useResizeDrag";

import "./workspace-surfaces.css";

/** The same resize gesture for touch, pen, mouse, and keyboard. */
export function ResizeHandle({
  axis,
  value,
  onChange,
  min,
  max,
  reset,
  label,
  className = "",
}: {
  axis: "x" | "y";
  value: number;
  onChange: (next: number) => void;
  min: number;
  max: number;
  reset: number;
  label: string;
  className?: string;
}) {
  const { onMouseDown, onPointerDown, isDragging } = useResizeDrag({
    axis, value, onChange, min, max,
    direction: "inverted",
    cursor: axis === "x" ? "col-resize" : "row-resize",
    lockUserSelect: true,
  });
  const clamp = (next: number) => onChange(Math.max(min, Math.min(max, next)));

  return (
    <div
      role="separator"
      aria-orientation={axis === "x" ? "vertical" : "horizontal"}
      aria-label={label}
      aria-valuenow={Math.round(value)}
      aria-valuemin={min}
      aria-valuemax={max}
      aria-valuetext={`${Math.round(value)} pixels`}
      tabIndex={0}
      title="Drag or use arrow keys to resize. Double-click to reset."
      className={`workspace-resize-handle resize-axis-${axis} ${isDragging ? "is-dragging" : ""} ${className}`}
      style={{ touchAction: "none" } as CSSProperties}
      onMouseDown={onMouseDown}
      onPointerDown={onPointerDown}
      onDoubleClick={() => clamp(reset)}
      onKeyDown={(event) => {
        const grow = axis === "x" ? "ArrowLeft" : "ArrowUp";
        const shrink = axis === "x" ? "ArrowRight" : "ArrowDown";
        if (event.key === grow || event.key === shrink) {
          event.preventDefault();
          clamp(value + (event.key === grow ? 1 : -1) * (event.shiftKey ? 48 : 16));
        } else if (event.key === "Home" || event.key === "End") {
          event.preventDefault();
          clamp(event.key === "Home" ? min : max);
        } else if (event.key === "Enter") {
          event.preventDefault();
          clamp(reset);
        }
      }}
    ><span aria-hidden="true" /></div>
  );
}
