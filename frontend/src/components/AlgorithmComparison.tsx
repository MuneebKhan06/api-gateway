import { useRef, useState } from "react";
import { gw } from "../api/client";
import type { RouteStatus } from "../api/types";
import { useSession } from "../session";
import { runBurst, summarize, type BurstSample } from "../utils/burst";
import { algorithmLabel, formatSeconds } from "../utils/format";
import BurstChart, { ChartLegend } from "./BurstChart";
import { toPoints } from "./BurstRunner";

interface AlgorithmComparisonProps {
  routes: RouteStatus[];
  onSample?: (route: RouteStatus, sample: BurstSample) => void;
}

/**
 * The same overload against every algorithm at once. Limits differ per
 * route, so each burst is sized relative to its own limit, and the charts are
 * small multiples rather than one chart with three scales.
 */
export default function AlgorithmComparison({ routes, onSample }: AlgorithmComparisonProps) {
  const { session } = useSession();
  const [overshoot, setOvershoot] = useState(20);
  const [samples, setSamples] = useState<Record<string, BurstSample[]>>({});
  const [running, setRunning] = useState(false);
  const abort = useRef<AbortController | null>(null);

  const start = async () => {
    if (!session) return;
    const controller = new AbortController();
    abort.current = controller;
    setSamples({});
    setRunning(true);
    try {
      await Promise.all(
        routes.map((route) => {
          const limit = route.rate_limit?.requests ?? 0;
          return runBurst({
            url: gw(route.path_prefix),
            token: session.accessToken,
            count: Math.ceil(limit * (1 + overshoot / 100)),
            concurrency: 8,
            spacingMs: 0,
            signal: controller.signal,
            onSample: (sample) => {
              setSamples((current) => ({
                ...current,
                [route.path_prefix]: [...(current[route.path_prefix] ?? []), sample],
              }));
              onSample?.(route, sample);
            },
          });
        }),
      );
    } finally {
      setRunning(false);
    }
  };

  // One x extent across the multiples, so their timing is comparable.
  const longest = Math.max(
    1,
    ...Object.values(samples).flatMap((list) => list.map((sample) => sample.sentAt / 1000)),
  );

  return (
    <section className="card">
      <div className="card-header">
        <h2>Compare the algorithms</h2>
        <span>all three routes at once, each pushed past its own limit</span>
      </div>

      <div className="burst-controls">
        <label>
          Past each limit by
          <select value={overshoot} onChange={(event) => setOvershoot(Number(event.target.value))}>
            <option value={10}>10 percent</option>
            <option value={20}>20 percent</option>
            <option value={50}>50 percent</option>
          </select>
        </label>
        {running ? (
          <button type="button" className="danger" onClick={() => abort.current?.abort()}>
            Stop
          </button>
        ) : (
          <button type="button" className="primary" onClick={start} disabled={!session}>
            Fire at all three
          </button>
        )}
        <span style={{ marginLeft: "auto" }}>
          <ChartLegend />
        </span>
      </div>

      <div className="grid grid-3" style={{ marginTop: 14 }}>
        {routes.map((route) => {
          const limit = route.rate_limit!;
          const list = samples[route.path_prefix] ?? [];
          const summary = summarize(list);
          return (
            <div key={route.path_prefix} className="stack comparison-cell">
              <div>
                <strong>{algorithmLabel(limit.algorithm)}</strong>
                <div className="hint">
                  <code>{route.path_prefix}</code>, {limit.requests} per{" "}
                  {formatSeconds(limit.window_seconds)}
                </div>
              </div>
              {list.length > 0 ? (
                <>
                  <BurstChart
                    points={toPoints(list)}
                    yMax={limit.requests}
                    xMax={longest}
                    height={180}
                    title={`${route.path_prefix} remaining`}
                    compact
                  />
                  <div className="hint">
                    <strong>{summary.allowed}</strong> allowed, <strong>{summary.limited}</strong>{" "}
                    refused of {summary.sent}
                    {summary.allowed === limit.requests && !running && ", exactly the limit"}
                  </div>
                </>
              ) : (
                <div className="empty">Not run yet.</div>
              )}
            </div>
          );
        })}
      </div>

      <p className="hint" style={{ marginBottom: 0 }}>
        With dozens of requests in flight, a GET, decide, SET limiter would let extras through on
        every race. Each check here is one Lua script, so the allowed count lands on the limit, plus
        any tokens the bucket refilled while the burst was running.
      </p>
    </section>
  );
}
