import { useEffect, useRef, useState } from "react";

import type { EquityBar } from "@/lib/api";
import { getLayoutScale } from "@/lib/layout-scale";
import { getThemeColors } from "@/lib/theme-colors";

export interface EquityChartProps {
  bars: EquityBar[];
  /** The held position's average entry price, if any — drawn as one price
   * line. No fill markers, no order lines: there's no order history or open
   * orders in scope yet, just the one thing a chart needs to say "you're
   * holding this, bought around here". */
  costBasis?: number | null;
}

/**
 * A candlestick chart for one equity symbol, built directly on
 * `lightweight-charts` rather than reusing `TradeChart`: that component
 * requires `server`/`connector` and derives its quote currency by splitting
 * a crypto pair on `-` (a bare ticker like `AAPL` has none), and its data
 * source is a WebSocket-fed singleton keyed by a channel shape equities
 * don't have — streaming is explicitly out of scope for this milestone.
 * This is fed by `bars`, a plain prop the page's own `useQuery` supplies.
 *
 * `import("lightweight-charts")` is dynamic, matching every other chart in
 * this app (`TradeChart`, `AgentPnlChart`) — not a style choice but what lets
 * `src/test/lightweight-charts-double.ts` substitute at the resolver for
 * every test file, this one included, with no per-file mock of its own.
 */
export function EquityChart({ bars, costBasis }: EquityChartProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartModuleRef = useRef<typeof import("lightweight-charts") | null>(null);
  const chartRef = useRef<import("lightweight-charts").IChartApi | null>(null);
  const seriesRef = useRef<import("lightweight-charts").ISeriesApi<"Candlestick"> | null>(null);
  const costBasisLineRef = useRef<import("lightweight-charts").IPriceLine | null>(null);
  const [ready, setReady] = useState(false);

  // ── Create once ──
  useEffect(() => {
    let cancelled = false;
    import("lightweight-charts").then((mod) => {
      if (cancelled || !containerRef.current) return;
      chartModuleRef.current = mod;
      const colors = getThemeColors();
      const scale = getLayoutScale();
      const chart = mod.createChart(containerRef.current, {
        autoSize: true,
        layout: {
          background: { type: mod.ColorType.Solid, color: colors.bg },
          textColor: colors.text,
          fontSize: 12 * scale,
        },
        grid: {
          vertLines: { color: colors.grid },
          horzLines: { color: colors.grid },
        },
        crosshair: { mode: mod.CrosshairMode.Normal },
        timeScale: { timeVisible: true, secondsVisible: false },
      });
      chartRef.current = chart;

      const series = chart.addSeries(mod.CandlestickSeries, {
        upColor: colors.up,
        downColor: colors.down,
        wickUpColor: colors.up,
        wickDownColor: colors.down,
        borderVisible: false,
      });
      seriesRef.current = series;
      setReady(true);
    });

    return () => {
      cancelled = true;
      chartRef.current?.remove();
      chartRef.current = null;
      seriesRef.current = null;
      costBasisLineRef.current = null;
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // ── Bars ──
  useEffect(() => {
    const series = seriesRef.current;
    if (!ready || !series) return;
    series.setData(
      bars.map((b) => ({
        time: b.timestamp as import("lightweight-charts").UTCTimestamp,
        open: b.open,
        high: b.high,
        low: b.low,
        close: b.close,
      })),
    );
    chartRef.current?.timeScale().fitContent();
  }, [ready, bars]);

  // ── Cost-basis line ──
  useEffect(() => {
    const series = seriesRef.current;
    const mod = chartModuleRef.current;
    if (!ready || !series || !mod) return;
    if (costBasisLineRef.current) {
      series.removePriceLine(costBasisLineRef.current);
      costBasisLineRef.current = null;
    }
    if (costBasis != null) {
      costBasisLineRef.current = series.createPriceLine({
        price: costBasis,
        color: getThemeColors().primary,
        lineWidth: 1,
        lineStyle: mod.LineStyle.Dashed,
        axisLabelVisible: true,
        title: "Cost basis",
      });
    }
  }, [ready, costBasis]);

  return <div ref={containerRef} className="h-full w-full min-h-[280px]" />;
}
