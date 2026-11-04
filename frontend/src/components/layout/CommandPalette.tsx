import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowRight, BookOpen, Bug, Command, Search, X } from "lucide-react";

import { NAV_ITEMS } from "@/lib/nav";

interface Props {
  open: boolean;
  onClose: () => void;
  onNavigate: (to: string) => void;
  onOpenGuide: () => void;
  onReportIssue: () => void;
}

interface CommandAction {
  id: string;
  label: string;
  path: string;
  icon: typeof Search;
  run: () => void;
}

/** Mounting a fresh dialog resets its search; closing restores the trigger's focus. */
export function CommandPalette({ open, ...props }: Props) {
  return open ? <CommandDialog {...props} /> : null;
}

/** Fast, keyboard-first navigation without adding a second router or data path. */
function CommandDialog({ onClose, onNavigate, onOpenGuide, onReportIssue }: Omit<Props, "open">) {
  const inputRef = useRef<HTMLInputElement>(null);
  const dialogRef = useRef<HTMLElement>(null);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const results = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    const actions: CommandAction[] = [
      ...NAV_ITEMS.map(({ to, icon, label }) => ({ id: to, label, path: to, icon, run: () => onNavigate(to) })),
      { id: "guide", label: "Get started guide", path: "Help", icon: BookOpen, run: onOpenGuide },
      { id: "report", label: "Report an issue", path: "Support", icon: Bug, run: onReportIssue },
    ];
    return actions.filter(({ label, path }) => `${label} ${path}`.toLowerCase().includes(normalized));
  }, [query, onNavigate, onOpenGuide, onReportIssue]);

  useEffect(() => {
    const frame = requestAnimationFrame(() => inputRef.current?.focus());
    const previousOverflow = document.body.style.overflow;
    const previousFocus = document.activeElement as HTMLElement | null;
    document.body.style.overflow = "hidden";
    return () => {
      cancelAnimationFrame(frame);
      document.body.style.overflow = previousOverflow;
      if (previousFocus?.isConnected) previousFocus.focus({ preventScroll: true });
    };
  }, []);

  const go = (action: CommandAction) => {
    action.run();
    onClose();
  };

  return (
    <div className="command-layer" role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget) onClose();
    }}>
      <section
        ref={dialogRef}
        className="command-palette"
        role="dialog"
        aria-modal="true"
        aria-label="Command menu"
        onKeyDown={(event) => {
          if (event.key === "Escape") onClose();
          if (event.key === "Tab") {
            const focusable = dialogRef.current?.querySelectorAll<HTMLElement>("input, button, [tabindex='0']");
            if (!focusable?.length) return;
            const first = focusable[0];
            const last = focusable[focusable.length - 1];
            if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
            if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
          }
          if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault();
            setActive((value) => results.length
              ? (value + (event.key === "ArrowDown" ? 1 : -1) + results.length) % results.length
              : 0);
          }
          if (event.key === "Enter" && results[active]) {
            event.preventDefault();
            go(results[active]);
          }
        }}
      >
        <div className="command-search">
          <Search aria-hidden="true" />
          <input
            ref={inputRef}
            value={query}
            onChange={(event) => { setQuery(event.target.value); setActive(0); }}
            placeholder="Where would you like to go?"
            aria-label="Search destinations"
            autoComplete="off"
          />
          <button className="icon-button subtle" onClick={onClose} aria-label="Close command menu">
            <X />
          </button>
        </div>
        <div className="command-results" role="listbox" aria-label="Destinations">
          <div className="command-section-label">Navigate</div>
          {results.map((action, index) => {
            const { id, icon: Icon, label, path } = action;
            return (
            <button
              key={id}
              role="option"
              aria-selected={index === active}
              className={`command-result ${index === active ? "is-active" : ""}`}
              onMouseEnter={() => setActive(index)}
              onClick={() => go(action)}
            >
              <span className="command-result-icon"><Icon /></span>
              <span>{label}</span>
              <span className="command-result-path">{path}</span>
              <ArrowRight className="command-result-arrow" />
            </button>
            );
          })}
          {results.length === 0 && (
            <div className="command-empty">No destinations match “{query}”.</div>
          )}
        </div>
        <footer className="command-footer">
          <span><kbd>↑</kbd><kbd>↓</kbd> to navigate</span>
          <span><kbd>↵</kbd> to open</span>
          <span className="ml-auto"><Command /> K</span>
        </footer>
      </section>
    </div>
  );
}
