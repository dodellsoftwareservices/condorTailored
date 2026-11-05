/**
 * EquityChart against the shared lightweight-charts double (CORR-368) — the
 * same substitution every other chart in this app gets, for free, because
 * the import is dynamic. See src/test/lightweight-charts-double.ts.
 *
 * @vitest-environment jsdom
 */

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { EquityBar } from "@/lib/api";
import { chartDouble } from "@/test/lightweight-charts-double";
import { EquityChart } from "./EquityChart";

declare global {
  // eslint-disable-next-line no-var
  var IS_REACT_ACT_ENVIRONMENT: boolean;
}

const BARS: EquityBar[] = [
  { timestamp: 1_700_000_000, open: 100, high: 102, low: 99, close: 101, volume: 1_000 },
  { timestamp: 1_700_086_400, open: 101, high: 104, low: 100, close: 103, volume: 1_200 },
];

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  chartDouble.reset();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

async function render(bars: EquityBar[], costBasis?: number | null) {
  await act(async () => {
    root.render(<EquityChart bars={bars} costBasis={costBasis} />);
  });
}

describe("EquityChart", () => {
  it("creates one candlestick series and loads the given bars", async () => {
    await render(BARS);

    expect(chartDouble.charts).toHaveLength(1);
    expect(chartDouble.charts[0]!.addedSeries).toEqual([{ seriesType: "Candlestick" }]);
    const setData = chartDouble.series?.setData as unknown as { mock: { calls: unknown[][] } };
    expect(setData.mock.calls.at(-1)![0]).toEqual([
      { time: 1_700_000_000, open: 100, high: 102, low: 99, close: 101 },
      { time: 1_700_086_400, open: 101, high: 104, low: 100, close: 103 },
    ]);
  });

  it("draws no cost-basis line when no position is held", async () => {
    await render(BARS, null);
    expect(chartDouble.series?.createPriceLine).not.toHaveBeenCalled();
  });

  it("draws exactly one cost-basis line, titled, at the entry price", async () => {
    await render(BARS, 102.5);

    expect(chartDouble.series?.createPriceLine).toHaveBeenCalledTimes(1);
    const call = (chartDouble.series?.createPriceLine as unknown as { mock: { calls: unknown[][] } })
      .mock.calls[0]![0] as { price: number; title: string };
    expect(call.price).toBe(102.5);
    expect(call.title).toBe("Cost basis");
  });

  it("re-sets data when the bars prop changes", async () => {
    await render(BARS);
    const setData = chartDouble.series?.setData as unknown as { mock: { calls: unknown[][] } };
    const callsBefore = setData.mock.calls.length;

    const more = [...BARS, { timestamp: 1_700_172_800, open: 103, high: 105, low: 102, close: 104, volume: 900 }];
    await act(async () => {
      root.render(<EquityChart bars={more} />);
    });

    expect(setData.mock.calls.length).toBeGreaterThan(callsBefore);
    expect(setData.mock.calls.at(-1)![0]).toHaveLength(3);
  });

  it("removes the chart on unmount", async () => {
    await render(BARS);
    const chart = chartDouble.chart;
    act(() => root.unmount());
    expect(chart?.remove).toHaveBeenCalledTimes(1);
  });
});
