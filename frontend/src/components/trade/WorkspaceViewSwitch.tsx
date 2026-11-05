import { ChartCandlestick, ListOrdered, SlidersHorizontal } from "lucide-react";

import "@/components/ui/workspace-surfaces.css";

export type WorkspaceView = "chart" | "trade" | "activity";

const VIEWS = [
  { value: "chart", label: "Chart", icon: ChartCandlestick },
  { value: "trade", label: "Trade", icon: SlidersHorizontal },
  { value: "activity", label: "Activity", icon: ListOrdered },
] as const;

/** CSS owns visibility so changing views preserves chart and order-form state. */
export function WorkspaceViewSwitch({ value, onChange }: {
  value: WorkspaceView;
  onChange: (value: WorkspaceView) => void;
}) {
  return (
    <div className="trade-view-switch" role="group" aria-label="Workspace view">
      {VIEWS.map(({ value: view, label, icon: Icon }) => (
        <button key={view} type="button" aria-pressed={view === value} onClick={() => onChange(view)}>
          <Icon size={15} aria-hidden="true" />{label}
        </button>
      ))}
    </div>
  );
}
