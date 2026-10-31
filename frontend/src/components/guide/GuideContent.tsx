import type { ComponentProps, MouseEvent } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

// The guide *is* docs/get-started.md, read at build time, so the walkthrough on
// GitHub and the one in the portal are one file. Editing the doc and rebuilding
// (`make run` does it) is the whole publishing step.
import doc from "../../../../docs/get-started.md?raw";

import { headingSlug, nodeText } from "@/lib/headingSlug";

type HeadingProps = ComponentProps<"h1">;

/** A heading with the id its `#anchor` links point at. */
function heading(Tag: "h1" | "h2" | "h3") {
  return function Heading({ children }: HeadingProps) {
    return (
      <Tag id={headingSlug(nodeText(children))} className="scroll-mt-4">
        {children}
      </Tag>
    );
  };
}

/**
 * Scroll the panel to a section without touching the address bar.
 *
 * A bare `#anchor` link would add a hash to whichever page is open behind the
 * guide, and a back-button entry for every click. The lookup is scoped to this
 * document so an id elsewhere in the app can never be the one that is found.
 */
function jump(e: MouseEvent<HTMLAnchorElement>, id: string) {
  e.preventDefault();
  e.currentTarget
    .closest(".docs-markdown")
    ?.querySelector(`[id="${id}"]`)
    ?.scrollIntoView({ behavior: "smooth", block: "start" });
}

const components: Components = {
  h1: heading("h1"),
  h2: heading("h2"),
  h3: heading("h3"),
  a({ href, children }) {
    if (href?.startsWith("#")) {
      return (
        <a href={href} onClick={(e) => jump(e, href.slice(1))}>
          {children}
        </a>
      );
    }
    if (href && /^https?:\/\//.test(href)) {
      return (
        <a href={href} target="_blank" rel="noreferrer">
          {children}
        </a>
      );
    }
    // Anything else is a path into the repo (`../README.md`). It is a link on
    // GitHub and nowhere in a browser tab, so it reads as plain text here.
    return <span>{children}</span>;
  },
};

export function GuideContent() {
  return (
    <div className="docs-markdown docs-markdown--compact">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {doc}
      </ReactMarkdown>
    </div>
  );
}
