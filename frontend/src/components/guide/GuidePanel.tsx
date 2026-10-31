import { BookOpen, X } from "lucide-react";

import { GuideContent } from "./GuideContent";

/**
 * The Get Started guide, docked to the right of whatever page is open.
 *
 * It is a sibling of `main` in the shell rather than a page of its own, so the
 * reader can follow the steps while doing them: switching tabs leaves it where
 * it is. From the `lg` breakpoint up it takes its 24rem out of the page's width;
 * below that there is no room to share, so it floats over the page instead.
 */
export function GuidePanel({ onClose }: { onClose: () => void }) {
  return (
    <aside
      id="guide-panel"
      aria-label="Get started guide"
      className="fixed bottom-0 right-0 top-12 z-40 flex w-full max-w-[24rem] flex-col border-l border-[var(--color-border)] bg-[var(--color-bg)] shadow-2xl lg:static lg:z-auto lg:w-96 lg:max-w-none lg:shrink-0 lg:shadow-none"
    >
      <div className="flex h-11 shrink-0 items-center justify-between border-b border-[var(--color-border)] px-4">
        <span className="flex items-center gap-2 text-xs font-medium uppercase tracking-wider text-[var(--color-text-muted)]">
          <BookOpen className="h-3.5 w-3.5" />
          Guide
        </span>
        <button
          onClick={onClose}
          className="rounded p-1 text-[var(--color-text-muted)] hover:bg-[var(--color-surface-hover)] hover:text-[var(--color-text)]"
          title="Close guide"
          aria-label="Close guide"
        >
          <X className="h-4 w-4" />
        </button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-5 pb-16 pt-5">
        <GuideContent />
      </div>
    </aside>
  );
}
