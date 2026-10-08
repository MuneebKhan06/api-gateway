import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { gw, request } from "../api/client";
import type { RouteStatus } from "../api/types";
import { useSession } from "../session";
import { formatMs } from "../utils/format";

// Steady traffic through one route, so the breaker has something to judge.
// Each result is classified by who answered, because the point of the
// breaker is the difference between "the upstream failed" and "the gateway
// refused without asking".

type Outcome = "ok" | "upstream_failed" | "breaker_refused" | "other";

interface Shot {
  id: number;
  at: number;
  status: number;
  outcome: Outcome;
  durationMs: number;
  breakerHeader: string | null;
}

const OUTCOME_LABELS: Record<Outcome, string> = {
  ok: "answered by upstream",
  upstream_failed: "upstream failed (counts against breaker)",
  breaker_refused: "refused by open breaker",
  other: "other (401, 429, 4xx)",
};

function classify(status: number, error: string | null): Outcome {
  if (error === "circuit_open") return "breaker_refused";
  if (status >= 200 && status < 400) return "ok";
  if (status >= 500 || status === 0) return "upstream_failed";
  return "other";
}

function average(values: number[]): number | null {
  return values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null;
}

const HISTORY = 60;

interface TrafficDriverProps {
  routes: RouteStatus[];
  /** The route to start on, so it matches the upstream the walkthrough follows. */
  defaultPath?: string;
}

export default function TrafficDriver({ routes, defaultPath }: TrafficDriverProps) {
  const { session } = useSession();
  const [path, setPath] = useState<string>("");
  const [intervalMs, setIntervalMs] = useState(1000);
  const [running, setRunning] = useState(false);
  const [shots, setShots] = useState<Shot[]>([]);
  const nextId = useRef(1);

  // Routes arrive after the first render, so settle the default once they do.
  useEffect(() => {
    if (!path) setPath(defaultPath ?? routes[0]?.path_prefix ?? "");
  }, [routes, path, defaultPath]);

  useEffect(() => {
    if (!running || !session) return;
    let cancelled = false;
    // One request in flight at a time: a slow upstream should slow the
    // driver down rather than pile requests up behind it.
    const loop = async () => {
      while (!cancelled) {
        const started = Date.now();
        const result = await request(gw(path), { token: session.accessToken });
        if (cancelled) break;
        const shot: Shot = {
          id: nextId.current++,
          at: started,
          status: result.status,
          outcome: classify(result.status, result.error?.error ?? null),
          durationMs: result.durationMs,
          breakerHeader: result.gateway.circuitBreaker,
        };
        setShots((current) => [...current, shot].slice(-HISTORY));
        const wait = intervalMs - (Date.now() - started);
        if (wait > 0) await new Promise((resolve) => window.setTimeout(resolve, wait));
      }
    };
    loop();
    return () => {
      cancelled = true;
    };
  }, [running, session, path, intervalMs]);

  const failedLatency = average(shots.filter((shot) => shot.outcome === "upstream_failed").map((shot) => shot.durationMs));
  const refusedLatency = average(shots.filter((shot) => shot.outcome === "breaker_refused").map((shot) => shot.durationMs));
  const last = shots[shots.length - 1];

  return (
    <section className="card">
      <div className="card-header">
        <h2>Send traffic</h2>
        <span>one request at a time through the gateway, with your token</span>
      </div>

      <div className="burst-controls">
        <label>
          Route
          <select value={path} onChange={(event) => setPath(event.target.value)}>
            {routes.map((route) => (
              <option key={route.path_prefix} value={route.path_prefix}>
                {route.path_prefix}
              </option>
            ))}
          </select>
        </label>
        <label>
          Every
          <select value={intervalMs} onChange={(event) => setIntervalMs(Number(event.target.value))}>
            <option value={500}>0.5 s</option>
            <option value={1000}>1 s</option>
            <option value={2000}>2 s</option>
          </select>
        </label>
        <button
          type="button"
          className={running ? "danger" : "primary"}
          disabled={!session || !path}
          onClick={() => setRunning((value) => !value)}
        >
          {running ? "Stop traffic" : "Start traffic"}
        </button>
        {shots.length > 0 && !running && (
          <button type="button" onClick={() => setShots([])}>
            Clear
          </button>
        )}
      </div>

      {!session && (
        <div className="callout" style={{ marginTop: 12 }}>
          Proxied routes need a token. <Link to="/auth">Sign in first</Link>.
        </div>
      )}

      <div className="shot-strip" role="list" aria-label="Recent requests, oldest first">
        {shots.map((shot) => (
          <span
            key={shot.id}
            role="listitem"
            className={`shot ${shot.outcome}`}
            title={`${shot.status} in ${formatMs(shot.durationMs)}, ${OUTCOME_LABELS[shot.outcome]}${shot.breakerHeader ? `, breaker ${shot.breakerHeader}` : ""}`}
          />
        ))}
        {shots.length === 0 && <span className="hint">Requests appear here, oldest on the left.</span>}
      </div>

      <div className="shot-legend">
        {(Object.keys(OUTCOME_LABELS) as Outcome[]).map((outcome) => (
          <span key={outcome}>
            <span className={`shot ${outcome}`} aria-hidden="true" />
            {OUTCOME_LABELS[outcome]}
          </span>
        ))}
      </div>

      {last && (
        <p className="hint" style={{ marginBottom: 0 }}>
          Last: <strong>{last.status}</strong> in {formatMs(last.durationMs)}
          {last.breakerHeader && (
            <>
              , <code>X-Circuit-Breaker: {last.breakerHeader}</code>
            </>
          )}
          .{" "}
          {failedLatency !== null && refusedLatency !== null && (
            <>
              Failed calls averaged {formatMs(failedLatency)}; refusals by the open breaker
              averaged {formatMs(refusedLatency)}, because they never touch the upstream.
            </>
          )}
        </p>
      )}
    </section>
  );
}
