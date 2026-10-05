import { useCallback, useEffect, useRef, useState } from "react";
import type { ApiResult } from "../api/types";

export interface Polled<T> {
  result: ApiResult<T> | null;
  loading: boolean;
  refresh: () => Promise<void>;
  updatedAt: number | null;
}

/**
 * Call a gateway endpoint now and every `intervalMs` after.
 *
 * Polling pauses while the tab is hidden: a console left open in a
 * background tab should not keep generating traffic that shows up in the
 * gateway's own metrics.
 */
export function usePolling<T>(
  fetcher: () => Promise<ApiResult<T>>,
  intervalMs: number,
): Polled<T> {
  const [result, setResult] = useState<ApiResult<T> | null>(null);
  const [loading, setLoading] = useState(true);
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const refresh = useCallback(async () => {
    const next = await fetcherRef.current();
    setResult(next);
    setUpdatedAt(Date.now());
    setLoading(false);
  }, []);

  useEffect(() => {
    let timer: number | undefined;
    let cancelled = false;
    let first = true;

    // The first call always goes out, so a page opened in a background tab
    // still has something to show when it is brought forward.
    const tick = async () => {
      if (first || !document.hidden) await refresh();
      first = false;
      if (!cancelled) timer = window.setTimeout(tick, intervalMs);
    };
    tick();

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [intervalMs, refresh]);

  return { result, loading, refresh, updatedAt };
}
