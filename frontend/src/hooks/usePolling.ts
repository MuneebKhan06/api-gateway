import { useCallback, useEffect, useRef, useState } from "react";

export interface Polled<R> {
  result: R | null;
  loading: boolean;
  refresh: () => Promise<void>;
  updatedAt: number | null;
}

/**
 * Call something now and every `intervalMs` after. Usually a gateway
 * endpoint, so R is usually an ApiResult.
 *
 * Polling pauses while the tab is hidden: a console left open in a
 * background tab should not keep generating traffic that shows up in the
 * gateway's own metrics.
 */
export function usePolling<R>(fetcher: () => Promise<R>, intervalMs: number): Polled<R> {
  const [result, setResult] = useState<R | null>(null);
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
