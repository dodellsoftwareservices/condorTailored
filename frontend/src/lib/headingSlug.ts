import { Children, isValidElement, type ReactNode } from "react";

/**
 * The anchor a heading gets, spelled the way GitHub spells it: lowercased,
 * punctuation dropped, spaces to hyphens. The Get Started doc links its own
 * sections with these (`#2-start-condor`), and it has to work both on GitHub and
 * in the page that renders it, so the page must produce the same ids.
 */
export function headingSlug(text: string): string {
  return text
    .trim()
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s-]/gu, "")
    .replace(/\s/g, "-");
}

/** The plain text of a heading's children, for slugging (`code` spans included). */
export function nodeText(node: ReactNode): string {
  return Children.toArray(node)
    .map((child) => {
      if (typeof child === "string" || typeof child === "number") return String(child);
      if (isValidElement<{ children?: ReactNode }>(child)) return nodeText(child.props.children);
      return "";
    })
    .join("");
}
