import type { UTCTimestamp } from "lightweight-charts";
import type { CandleData } from "./api";
import { CHART_PREFERENCES_KEY } from "./sessionState";

export interface ChartPreferences {
  volume: boolean;
  sma: boolean;
  ema: boolean;
  logarithmic: boolean;
}

const DEFAULTS: ChartPreferences = { volume: true, sma: false, ema: false, logarithmic: false };

export function loadChartPreferences(): ChartPreferences {
  try {
    const saved = JSON.parse(localStorage.getItem(CHART_PREFERENCES_KEY) || "null");
    return Object.fromEntries(Object.entries(DEFAULTS).map(([key, value]) =>
      [key, typeof saved?.[key] === "boolean" ? saved[key] : value],
    )) as unknown as ChartPreferences;
  } catch { return { ...DEFAULTS }; }
}

export function saveChartPreferences(preferences: ChartPreferences) {
  try { localStorage.setItem(CHART_PREFERENCES_KEY, JSON.stringify(preferences)); } catch { /* optional persistence */ }
}

/** One bad upstream bar must not leave the entire canvas blank. */
export function chartCandles(rows: CandleData[]): CandleData[] {
  const byTime = new Map<number, CandleData>();
  for (const row of rows) {
    const timestamp = Math.floor(row.timestamp > 1e12 ? row.timestamp / 1000 : row.timestamp);
    if (![timestamp, row.open, row.high, row.low, row.close].every(Number.isFinite)
      || timestamp <= 0 || Math.min(row.open, row.high, row.low, row.close) <= 0
      || row.high < Math.max(row.open, row.close) || row.low > Math.min(row.open, row.close)) continue;
    byTime.set(timestamp, { ...row, timestamp, volume: Number.isFinite(row.volume) ? Math.max(0, row.volume) : 0 });
  }
  return [...byTime.values()].sort((a, b) => a.timestamp - b.timestamp);
}

/** EMA is seeded with a full-period SMA; no invented warm-up values. */
export function movingAverage(candles: CandleData[], period: number, kind: "sma" | "ema") {
  if (!Number.isInteger(period) || period < 1) return [];
  const points: { time: UTCTimestamp; value: number }[] = [];
  let sum = 0;
  let previous = 0;
  const alpha = 2 / (period + 1);
  for (let i = 0; i < candles.length; i++) {
    sum += candles[i].close;
    if (i >= period) sum -= candles[i - period].close;
    if (i < period - 1) continue;
    const value = kind === "sma" || i === period - 1 ? sum / period : candles[i].close * alpha + previous * (1 - alpha);
    points.push({ time: candles[i].timestamp as UTCTimestamp, value });
    previous = value;
  }
  return points;
}

export function chartFeedError(error: unknown) {
  const message = error instanceof Error ? error.message : "The market-data service could not be reached.";
  const restricted = /restricted location|eligibility|\b451\b|geo.?block/i.test(message);
  return {
    restricted,
    message: restricted
      ? "This venue does not serve market data from your location. Choose another public market to chart."
      : message,
  };
}
