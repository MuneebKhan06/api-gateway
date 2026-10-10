import { useMemo, useState } from "react";
import { ALGORITHMS, replay, spread, worstWindow, type Algorithm } from "../utils/simulate";
import { algorithmLabel } from "../utils/format";
import BurstChart, { ChartLegend } from "./BurstChart";

// The fixed window boundary problem, replayed through ports of all three Lua
// scripts. Small numbers on purpose: 10 per 10 seconds shows the same shape
// as 100 per minute, and it fits on a chart.

const WINDOW = 10;

/**
 * One request opens the window at t=0, then a client waits until just before
 * it closes and sends a full batch, and another full batch just after.
 */
function pattern(limit: number, gap: number): number[] {
  const before = spread(limit, WINDOW - gap - 0.9, WINDOW - gap);
  const after = spread(limit, WINDOW + 0.05, WINDOW + 0.95);
  return [0, ...before, ...after];
}

export default function BoundaryDemo() {
  const [limit, setLimit] = useState(10);
  const times = useMemo(() => pattern(limit, 0.05), [limit]);

  const results = useMemo(
    () =>
      ALGORITHMS.map((algorithm: Algorithm) => {
        const decisions = replay(algorithm, limit, WINDOW, times);
        return {
          algorithm,
          decisions,
          worst: worstWindow(decisions, WINDOW),
          allowed: decisions.filter((decision) => decision.allowed).length,
        };
      }),
    [limit, times],
  );

  return (
    <section className="card">
      <div className="card-header">
        <h2>The fixed window boundary problem</h2>
        <span>simulated, using ports of the gateway's Lua scripts</span>
      </div>
      <p className="hint" style={{ marginTop: 0 }}>
        A client sends one request, waits until the window is about to reset, then sends{" "}
        {limit} just before and {limit} just after. The limit says {limit} per {WINDOW}s, so no{" "}
        {WINDOW} second stretch should ever see more than {limit} accepted.
      </p>

      <div className="burst-controls">
        <label>
          Limit per {WINDOW}s
          <select value={limit} onChange={(event) => setLimit(Number(event.target.value))}>
            {[5, 10, 20].map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </label>
        <span style={{ marginLeft: "auto" }}>
          <ChartLegend />
        </span>
      </div>

      <div className="grid grid-3" style={{ marginTop: 14 }}>
        {results.map((result) => {
          const over = result.worst > limit;
          // Token bucket promises a sustained rate plus a burst, not a hard
          // cap per window, so going over by what refilled is by design.
          const verdict = !over
            ? { tone: "ok", text: "limit held" }
            : result.algorithm === "token_bucket"
              ? { tone: "warn", text: "burst plus refill" }
              : { tone: "bad", text: "limit broken" };
          return (
            <div key={result.algorithm} className="stack comparison-cell">
              <div className="row" style={{ justifyContent: "space-between" }}>
                <strong>{algorithmLabel(result.algorithm)}</strong>
                <span className={`pill ${verdict.tone}`}>{verdict.text}</span>
              </div>
              <BurstChart
                points={result.decisions.map((decision, index) => ({
                  x: decision.at,
                  y: decision.remaining,
                  allowed: decision.allowed,
                  label: `request ${index + 1}`,
                }))}
                yMax={limit}
                xMax={WINDOW + 1.5}
                height={180}
                title={`${algorithmLabel(result.algorithm)} remaining`}
                compact
                markers={result.algorithm === "fixed_window" ? [{ x: WINDOW, label: "reset" }] : []}
              />
              <div className="hint">
                Most accepted in any {WINDOW}s: <strong>{result.worst}</strong> (limit {limit})
              </div>
            </div>
          );
        })}
      </div>

      <p className="hint" style={{ marginBottom: 0 }}>
        Fixed window counts each window on its own, so both batches fit and nearly twice the limit
        lands inside two seconds. Sliding window looks back a full {WINDOW}s from every request.
        Token bucket goes over by the little that refilled during the window, which is its
        contract: a burst up to capacity, then the sustained rate. The sliding window suite has a
        test that fires a burst across a boundary and asserts it is caught.
      </p>
    </section>
  );
}
