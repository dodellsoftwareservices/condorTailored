import { Loader2 } from "lucide-react";

import "./workspace-surfaces.css";

export function FallbackSpinner() {
  return (
    <div className="workspace-loading" role="status" aria-live="polite">
      <Loader2 className="h-5 w-5 animate-spin" aria-hidden="true" />
      <span className="workspace-loading-label">Loading workspace…</span>
      <div className="workspace-loading-track" aria-hidden="true" />
    </div>
  );
}
