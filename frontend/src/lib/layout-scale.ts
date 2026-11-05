/** Convert viewport/pointer coordinates to the portal's CSS layout pixels.
 * Browser zoom already adjusts CSSOM coordinates; body CSS zoom does not.
 * Keep user browser zoom independent and account only for the app's zoom.
 */
export function getLayoutScale(): number {
  if (typeof document === "undefined") return 1;
  const scale = Number.parseFloat(getComputedStyle(document.body).zoom);
  return Number.isFinite(scale) && scale > 0 ? scale : 1;
}

export function getLayoutViewport(scale = getLayoutScale()) {
  return { width: window.innerWidth / scale, height: window.innerHeight / scale };
}

/** A canvas surface may cancel body zoom to keep native chart input aligned. */
export function getElementScale(element: HTMLElement): number {
  const effectiveScale = element.currentCSSZoom;
  if (Number.isFinite(effectiveScale) && effectiveScale > 0) return effectiveScale;
  const width = element.offsetWidth;
  if (width > 0) return element.getBoundingClientRect().width / width;
  return getLayoutScale();
}

export function toLayoutRect(
  rect: Pick<DOMRect, "left" | "right" | "top" | "bottom" | "width" | "height">,
  scale = getLayoutScale(),
) {
  return {
    left: rect.left / scale,
    right: rect.right / scale,
    top: rect.top / scale,
    bottom: rect.bottom / scale,
    width: rect.width / scale,
    height: rect.height / scale,
  };
}

// The same 90%-adjusted breakpoints as index.css and shell.css.
export const MOBILE_MEDIA_QUERY = "(width < 921.6px)";
export const WIDE_MEDIA_QUERY = "(min-width: 1152px)";
