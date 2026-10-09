import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import {
  bucketDelta,
  buckets,
  delta,
  histogramQuantile,
  parseExposition,
  sum,
  type LabelFilter,
  type Scrape,
} from "../utils/prometheus";

export const SCRAPE_INTERVAL_MS = 3000;
/** Two minutes of history at the scrape interval. */
const HISTORY = 40;
/** Rates are taken over this window, like rate(x[15s]). */
export const RATE_WINDOW_MS = 15000;

export interface Metrics {
  latest: Scrape | null;
  history: Scrape[];
  error: string | null;
  /** Per second increase of a counter over the rate window. */
  rate: (name: string, filter?: LabelFilter) => number | null;
  /** Per second increase between each pair of scrapes, oldest first. */
  rateSeries: (name: string, filter?: LabelFilter) => number[];
  /**
   * A quantile over the rate window, in seconds. Falls back to all time
   * when nothing was observed in the window, and says which it used.
   */
  quantile: (
    q: number,
    histogram: string,
    filter?: LabelFilter,
  ) => { value: number | null; recent: boolean };
}

/**
 * Scrape /metrics on an interval and keep a short history, so counters can be
 * turned into rates the way Prometheus would. The gateway exposes the raw
 * counters only; everything per second is computed here.
 */
export function useMetrics(): Metrics {
  const [history, setHistory] = useState<Scrape[]>([]);
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<number | undefined>(undefined);

  const scrape = useCallback(async () => {
    const result = await api.metrics();
    if (!result.ok) {
      setError(result.networkError ? "The gateway is not answering." : `/metrics returned ${result.status}`);
      return;
    }
    setError(null);
    const parsed = parseExposition(result.rawBody);
    setHistory((current) => [...current, parsed].slice(-HISTORY));
  }, []);

  useEffect(() => {
    let cancelled = false;
    let first = true;
    const tick = async () => {
      if (first || !document.hidden) await scrape();
      first = false;
      if (!cancelled) timer.current = window.setTimeout(tick, SCRAPE_INTERVAL_MS);
    };
    tick();
    return () => {
      cancelled = true;
      window.clearTimeout(timer.current);
    };
  }, [scrape]);

  const latest = history.length > 0 ? history[history.length - 1] : null;

  /** The oldest scrape still inside the rate window, if any is older than latest. */
  const windowStart = (): Scrape | null => {
    if (!latest || history.length < 2) return null;
    const cutoff = latest.at - RATE_WINDOW_MS;
    return history.find((entry) => entry.at >= cutoff && entry !== latest) ?? history[history.length - 2];
  };

  const rate = (name: string, filter?: LabelFilter): number | null => {
    const start = windowStart();
    if (!start || !latest) return null;
    const seconds = (latest.at - start.at) / 1000;
    if (seconds <= 0) return null;
    return delta(sum(start, name, filter), sum(latest, name, filter)) / seconds;
  };

  const rateSeries = (name: string, filter?: LabelFilter): number[] => {
    const series: number[] = [];
    for (let index = 1; index < history.length; index += 1) {
      const before = history[index - 1];
      const after = history[index];
      const seconds = (after.at - before.at) / 1000;
      series.push(seconds > 0 ? delta(sum(before, name, filter), sum(after, name, filter)) / seconds : 0);
    }
    return series;
  };

  const quantile = (q: number, histogram: string, filter?: LabelFilter) => {
    if (!latest) return { value: null, recent: false };
    const now = buckets(latest, histogram, filter);
    const start = windowStart();
    if (start) {
      const recent = histogramQuantile(q, bucketDelta(buckets(start, histogram, filter), now));
      if (recent !== null) return { value: recent, recent: true };
    }
    return { value: histogramQuantile(q, now), recent: false };
  };

  return { latest, history, error, rate, rateSeries, quantile };
}
