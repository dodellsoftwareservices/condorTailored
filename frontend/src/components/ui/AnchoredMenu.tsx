import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { useDismissOnOutsideClick } from "@/hooks/useDismissOnOutsideClick";
import { useEscapeKey } from "@/hooks/useEscapeKey";
import { getLayoutScale, getLayoutViewport, toLayoutRect } from "@/lib/layout-scale";

import "./workspace-surfaces.css";

interface Props {
  /** The button the panel hangs off. */
  anchor: HTMLElement | null;
  open: boolean;
  onClose: () => void;
  /** Which edge of the panel lines up with the same edge of the anchor. */
  align?: "left" | "right";
  /**
   * Tie the panel's width to its trigger, which `w-full` cannot do once the
   * panel is portalled — 100% would be of `document.body`. `"exact"` for a
   * select, whose menu must line up with its field; `"min"` for a menu that
   * may outgrow its trigger but never be narrower.
   */
  matchAnchorWidth?: "exact" | "min";
  /**
   * A px ceiling applied on top of the measured room, for a menu that is meant
   * to stay compact even when the viewport could fit more.
   */
  maxHeight?: number;
  /**
   * Distance between the trigger and the panel, in px. Defaults to a floating
   * 6; a menu drawn as a continuation of its trigger passes ~1.
   */
  gap?: number;
  /** Width utility for the panel, e.g. `w-44`. */
  className?: string;
  role?: React.AriaRole;
  children: React.ReactNode;
}

/** Default distance between trigger and panel, and between panel and viewport edge. */
const DEFAULT_GAP = 6;
const EDGE = 8;
/**
 * Below this much room the panel would open as a useless sliver, so it is worth
 * flipping above the trigger instead — the case that matters is the last field
 * in a config panel, which sits just above the launch footer.
 */
const MIN_ROOM = 160;

/**
 * A dropdown panel that no ancestor can clip.
 *
 * An `absolute` menu is still a child of its trigger, so any `overflow-hidden`
 * on the way up cuts it off. That is exactly what happened to the DEX chain and
 * venue pickers: the browser wraps its header and pool table in one clipping
 * card, and on a chain with no pools that card is barely taller than the header
 * itself — so most of the chain list was invisible and unclickable, on the very
 * screen where switching chains is the only thing left to do.
 *
 * A scrolling ancestor fails differently and just as badly. The executor and LP
 * config forms live in a `flex-1 overflow-y-auto` panel: an absolute menu there
 * does extend the scroller's scrollHeight, so the options are reachable — but
 * nothing scrolls for you, so a user already at the bottom of the panel opens
 * the select and sees a few pixels of list and no cue that more scroll room
 * just appeared.
 *
 * Rendering into `document.body` at fixed coordinates escapes every clipping
 * and scrolling ancestor. The trade is that the panel no longer moves with the
 * page, so it is re-anchored on scroll and resize instead — and that tab order
 * no longer follows visual order, which is why the panel also takes focus on
 * open and hands it back on close (see the focus effect below).
 */
export function AnchoredMenu({
  anchor,
  open,
  onClose,
  align = "left",
  matchAnchorWidth,
  maxHeight,
  gap = DEFAULT_GAP,
  className = "",
  role,
  children,
}: Props) {
  const menuRef = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{
    top?: number;
    bottom?: number;
    left?: number;
    right?: number;
    maxHeight: number;
    maxWidth: number;
    width?: number;
    minWidth?: number;
  } | null>(null);
  const placed = pos !== null;

  // Stale coordinates from a previous open are harmless: the panel renders
  // nothing while closed, and this runs before the browser paints the reopen.
  useLayoutEffect(() => {
    if (!open || !anchor) return;
    const place = () => {
      const scale = getLayoutScale();
      const layoutViewport = getLayoutViewport(scale);
      const r = toLayoutRect(anchor.getBoundingClientRect(), scale);
      // A phone keyboard and pinch zoom change the visible viewport without
      // changing layout coordinates. Keep the whole menu in the visible area.
      const viewport = window.visualViewport;
      const leftEdge = (viewport?.offsetLeft ?? 0) / scale + EDGE;
      const topEdge = (viewport?.offsetTop ?? 0) / scale + EDGE;
      const rightEdge = ((viewport?.offsetLeft ?? 0) + (viewport?.width ?? window.innerWidth)) / scale - EDGE;
      const bottomEdge = ((viewport?.offsetTop ?? 0) + (viewport?.height ?? window.innerHeight)) / scale - EDGE;
      const availableWidth = Math.max(0, rightEdge - leftEdge);
      const anchorTop = Math.max(topEdge, Math.min(bottomEdge, r.top));
      const anchorBottom = Math.max(topEdge, Math.min(bottomEdge, r.bottom));
      const below = bottomEdge - anchorBottom - gap;
      const above = anchorTop - gap - topEdge;
      // Open upward only when downward is cramped *and* upward is roomier —
      // otherwise the familiar direction wins.
      const cap = (room: number) =>
        Math.max(maxHeight ? Math.min(room, maxHeight) : room, 0);
      const vertical =
        below < Math.min(MIN_ROOM, above)
          ? { bottom: layoutViewport.height - anchorTop + gap, maxHeight: cap(above) }
          : { top: anchorBottom + gap, maxHeight: cap(below) };
      const measuredWidth = menuRef.current?.offsetWidth || (menuRef.current ? menuRef.current.getBoundingClientRect().width / scale : r.width);
      const panelWidth = Math.min(measuredWidth, availableWidth);
      const horizontal =
        align === "right"
          ? { right: layoutViewport.width - Math.max(leftEdge + panelWidth, Math.min(rightEdge, r.right)) }
          : { left: Math.max(leftEdge, Math.min(r.left, rightEdge - panelWidth)) };
      setPos({
        ...vertical,
        ...horizontal,
        maxWidth: availableWidth,
        width: matchAnchorWidth === "exact" ? Math.min(r.width, availableWidth) : undefined,
        minWidth: matchAnchorWidth === "min" ? Math.min(r.width, availableWidth) : undefined,
      });
    };
    place();
    // Capture phase: the page's own scroll containers do not bubble scroll.
    window.addEventListener("scroll", place, true);
    window.addEventListener("resize", place);
    window.visualViewport?.addEventListener("resize", place);
    window.visualViewport?.addEventListener("scroll", place);
    return () => {
      window.removeEventListener("scroll", place, true);
      window.removeEventListener("resize", place);
      window.visualViewport?.removeEventListener("resize", place);
      window.visualViewport?.removeEventListener("scroll", place);
    };
  }, [open, anchor, align, matchAnchorWidth, maxHeight, gap, placed]);

  // Tab order follows the DOM, and the DOM now says the panel is a sibling of
  // the whole app rather than a child of its trigger. Without this, Tab from an
  // open menu walks into whatever control comes next *on the page*, leaving the
  // options unreachable from the keyboard. Runs after the layout effect above
  // on purpose: the panel is not in the DOM until `pos` is set.
  useEffect(() => {
    if (!open || !placed) return;
    const panel = menuRef.current;
    if (!panel) return;
    // A panel that autofocuses something of its own — PairSelector's search
    // box, the venue filter — has already claimed focus by now, and stealing it
    // back to the wrapper would eat the first keystroke.
    if (!panel.contains(document.activeElement)) {
      // The layout effect just positioned it; scrolling it into view would
      // only fight that.
      panel.focus({ preventScroll: true });
    }
    return () => {
      // Restore only when the panel was still the thing holding focus: removing
      // the focused node drops focus to <body>, whereas a mousedown that landed
      // on some other control has already moved focus there, and pulling it
      // back to the trigger would undo the user's own click.
      const active = document.activeElement;
      if (active && active !== document.body) return;
      if (anchor?.isConnected) anchor.focus({ preventScroll: true });
    };
  }, [open, placed, anchor]);

  useDismissOnOutsideClick(open, onClose, [menuRef, anchor]);
  useEscapeKey(open, onClose);

  if (!open || !pos) return null;

  return createPortal(
    <div
      ref={menuRef}
      role={role}
      // Focusable on open without joining the tab order itself, so Tab from the
      // panel steps into the options rather than skipping the menu entirely.
      tabIndex={-1}
      style={{
        position: "fixed",
        top: pos.top,
        bottom: pos.bottom,
        left: pos.left,
        right: pos.right,
        // Never taller than the room on the side it opened to, so a long list
        // scrolls inside the panel rather than running off the window.
        maxHeight: pos.maxHeight,
        maxWidth: pos.maxWidth,
        width: pos.width,
        minWidth: pos.minWidth,
      }}
      className={`workspace-popover anchored-menu z-50 overflow-y-auto rounded-xl border border-[var(--color-border)] focus:outline-none bg-[var(--color-surface)] shadow-xl ${className}`}
    >
      {children}
    </div>,
    document.body,
  );
}
