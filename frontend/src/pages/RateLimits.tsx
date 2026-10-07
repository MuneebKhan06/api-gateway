import { useState } from "react";
import { api } from "../api/client";
import type { RouteStatus } from "../api/types";
import AlgorithmComparison from "../components/AlgorithmComparison";
import BenchmarkResults from "../components/BenchmarkResults";
import BoundaryDemo from "../components/BoundaryDemo";
import BurstRunner from "../components/BurstRunner";
import { usePolling } from "../hooks/usePolling";
import type { BurstSample } from "../utils/burst";
import { ALGORITHM_SUMMARY, algorithmLabel, formatSeconds } from "../utils/format";

export default function RateLimits() {
  const routes = usePolling(api.routes, 15000);
  const limited = (routes.result?.data ?? []).filter(
    (route) => route.rate_limit !== null && route.upstream !== null,
  );
  const [selected, setSelected] = useState<string | null>(null);
  const [remaining, setRemaining] = useState<Record<string, BurstSample>>({});

  const active = limited.find((route) => route.path_prefix === selected) ?? limited[0] ?? null;

  const recordSample = (route: RouteStatus, sample: BurstSample) =>
    setRemaining((current) => {
      const previous = current[route.path_prefix];
      // Responses arrive out of order under concurrency; keep the latest sent.
      if (previous && previous.index > sample.index) return current;
      return { ...current, [route.path_prefix]: sample };
    });

  return (
    <>
      <div className="page-header">
        <h1>Rate limiting</h1>
        <p>
          Every proxied route runs one of three algorithms, each a single Lua script executed
          atomically in Redis, so concurrent requests can never both read the same counter and
          both get through. Fire real traffic at a route and watch its allowance drain.
        </p>
      </div>

      <div className="grid grid-3">
        {limited.map((route) => {
          const limit = route.rate_limit!;
          const last = remaining[route.path_prefix];
          const isActive = route.path_prefix === active?.path_prefix;
          return (
            <button
              key={route.path_prefix}
              type="button"
              className={`card route-card${isActive ? " active" : ""}`}
              onClick={() => setSelected(route.path_prefix)}
            >
              <div className="row" style={{ justifyContent: "space-between", width: "100%" }}>
                <code>{route.path_prefix}</code>
                <span className="pill info plain">{algorithmLabel(limit.algorithm)}</span>
              </div>
              <div className="route-limit">
                {limit.requests} <span>per {formatSeconds(limit.window_seconds)}</span>
              </div>
              <div className="hint">{ALGORITHM_SUMMARY[limit.algorithm]}</div>
              <div className="hint mono">
                {last
                  ? `last seen: ${last.remaining ?? "?"} of ${last.limit ?? limit.requests} left`
                  : `${limit.requests_per_second}/s sustained`}
              </div>
            </button>
          );
        })}
      </div>

      {active ? (
        <section className="card">
          <div className="card-header">
            <h2>Fire a burst</h2>
            <span>
              {algorithmLabel(active.rate_limit!.algorithm)} on <code>{active.path_prefix}</code>
            </span>
          </div>
          <BurstRunner key={active.path_prefix} route={active} onSample={recordSample} />
        </section>
      ) : (
        <div className="card empty">No rate limited routes reported by the gateway.</div>
      )}

      {limited.length > 1 && <AlgorithmComparison routes={limited} onSample={recordSample} />}
      <BoundaryDemo />
      <BenchmarkResults />
    </>
  );
}
