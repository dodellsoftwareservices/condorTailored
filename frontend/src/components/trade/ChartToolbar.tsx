import { BarChart3, Camera, Crosshair, Expand, MoveRight, Ruler, ScanLine } from "lucide-react";
import type { ChartPreferences } from "@/lib/chart-analysis";

interface Props {
  preferences: ChartPreferences;
  onToggle: (key: keyof ChartPreferences) => void;
  measuring: boolean;
  expanded: boolean;
  onMeasure: () => void;
  onFit: () => void;
  onLatest: () => void;
  onSnapshot: () => void;
  onExpand: () => void;
  ready: boolean;
}

export function ChartToolbar({ preferences, onToggle, measuring, expanded, onMeasure, onFit, onLatest, onSnapshot, onExpand, ready }: Props) {
  return (
    <div className="chart-toolbar" role="toolbar" aria-label="Chart tools">
      <span className="chart-toolbar-mark" title="Interactive price chart"><Crosshair size={14} /></span>
      <button type="button" aria-label="Show volume" aria-pressed={preferences.volume} onClick={() => onToggle("volume")} title="Volume bars"><BarChart3 size={14} /><span>Volume</span></button>
      <button type="button" aria-label="Show SMA 20" aria-pressed={preferences.sma} onClick={() => onToggle("sma")} title="20-period simple moving average">SMA <span>20</span></button>
      <button type="button" aria-label="Show EMA 50" aria-pressed={preferences.ema} onClick={() => onToggle("ema")} title="50-period exponential moving average">EMA <span>50</span></button>
      <span className="chart-toolbar-divider" />
      <button type="button" aria-label="Measure price range" aria-pressed={measuring} onClick={onMeasure} title="Measure a range: click to anchor, move to measure; Esc clears"><Ruler size={14} /></button>
      <div className="chart-toolbar-spacer" />
      <button type="button" aria-label="Logarithmic price scale" aria-pressed={preferences.logarithmic} onClick={() => onToggle("logarithmic")} title="Logarithmic price scale">Log</button>
      <button type="button" aria-label="Fit chart" onClick={onFit} disabled={!ready} title="Fit all loaded candles"><ScanLine size={14} /></button>
      <button type="button" aria-label="Go to latest candle" onClick={onLatest} disabled={!ready} title="Go to latest candle"><MoveRight size={14} /></button>
      <button type="button" aria-label="Save chart image" onClick={onSnapshot} disabled={!ready} title="Save chart as PNG"><Camera size={14} /></button>
      <button type="button" aria-label={expanded ? "Exit expanded chart" : "Expand chart"} aria-pressed={expanded} onClick={onExpand} title={expanded ? "Exit expanded chart (Esc)" : "Expand chart"}><Expand size={14} /></button>
    </div>
  );
}
