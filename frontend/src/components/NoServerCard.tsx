import { ArrowRight, Server } from "lucide-react";
import { useNavigate } from "react-router-dom";

import "@/components/ui/workspace-surfaces.css";

/** Shared empty-state card shown when no server is selected. */
export function NoServerCard({
  message = "Choose a server from the toolbar, or connect one in Settings to get started.",
}: {
  message?: string;
}) {
  const navigate = useNavigate();

  return (
    <div className="workspace-empty">
      <div className="workspace-empty-card">
        <div className="workspace-empty-icon" aria-hidden="true"><Server size={24} strokeWidth={1.5} /></div>
        <h2 className="text-lg font-semibold mb-1">No Server Selected</h2>
        <p className="text-sm text-[var(--color-text-muted)]">{message}</p>
        <button
          onClick={() => navigate("/settings?tab=servers")}
          className="workspace-button"
        >
          Go to Settings → Servers
          <ArrowRight size={14} aria-hidden="true" />
        </button>
      </div>
    </div>
  );
}
