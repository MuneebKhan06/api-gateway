import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { gw } from "../api/client";
import type { RouteStatus } from "../api/types";
import { useSession } from "../session";
import { runBurst, summarize, type BurstSample } from "../utils/burst";
import { algorithmLabel, formatSeconds } from "../utils/format";
import BurstChart, { type ChartPoint } from "./BurstChart";

type Mode = "burst" | "paced";

export function toPoints(samples: BurstSample[]): ChartPoint[] {
  return samples.map((sample) => ({
    x: sample.sentAt / 1000,
    y: sample.remaining ?? 0,
    allowed: sample.allowed,
    label: `#${sample.index + 1}, ${sample.status}${sample.error ? ` ${sample.error}` : ""}`,
  }));
}

/** What happened, in words, for the algorithm that produced it. */
function verdict(route: RouteStatus, samples: BurstSample[], mode: Mode): string {
  const summary = summarize(samples);
  const limit = route.rate_limit?.requests ?? 0;
  const algorithm = route.rate_limit?.algorithm;
  if (summary.limited === 0) {
    return `All ${summary.allowed} requests were allowed. Send more than the limit of ${limit}, or less spread out, to see the limiter step in.`;
  }
  const first = `The first refusal was request #${(summary.firstLimitedIndex ?? 0) + 1}.`;
  if (mode === "paced" && algorithm === "token_bucket") {
    return `${first} Paced traffic keeps getting through because the bucket refills at ${route.rate_limit?.requests_per_second}/s: once it is empty, a request is allowed each time a whole token has accrued.`;
  }
  if (algorithm === "token_bucket") {
    return `${first} ${summary.allowed} allowed out of ${summary.sent}: the full bucket of ${limit} as one burst, plus whatever refilled while it ran. That burst allowance is why this is the default.`;
  }
  if (algorithm === "sliding_window") {
    return `${first} ${summary.allowed} allowed. The log holds one entry per accepted request, and a slot frees only when the oldest entry is ${route.rate_limit?.window_seconds}s old, so Retry-After is exact.`;
  }
  return `${first} ${summary.allowed} allowed. The counter resets all at once when its ${route.rate_limit?.window_seconds}s window ends, which is simple and cheap and is also the source of the boundary problem below.`;
}

interface BurstRunnerProps {
  route: RouteStatus;
  /** Called with every sample, so the page can show live remaining per route. */
  onSample?: (route: RouteStatus, sample: BurstSample) => void;
}

export default function BurstRunner({ route, onSample }: BurstRunnerProps) {
  const { session } = useSession();
  const limit = route.rate_limit?.requests ?? 0;
  const [count, setCount] = useState(limit + 10);
  const [mode, setMode] = useState<Mode>("burst");
  const [concurrency, setConcurrency] = useState(10);
  const [spacingMs, setSpacingMs] = useState(250);
  const [samples, setSamples] = useState<BurstSample[]>([]);
  const [running, setRunning] = useState(false);
  const [ranMode, setRanMode] = useState<Mode>("burst");
  const abort = useRef<AbortController | null>(null);

  const start = async () => {
    if (!session) return;
    const controller = new AbortController();
    abort.current = controller;
    setSamples([]);
    setRunning(true);
    setRanMode(mode);
    try {
      await runBurst({
        url: gw(route.path_prefix),
        token: session.accessToken,
        count,
        concurrency,
        spacingMs: mode === "paced" ? spacingMs : 0,
        signal: controller.signal,
        onSample: (sample) => {
          setSamples((current) => [...current, sample]);
          onSample?.(route, sample);
        },
      });
    } finally {
      setRunning(false);
    }
  };

  const stop = () => abort.current?.abort();
  const summary = summarize(samples);
  const pacedSeconds = (count * spacingMs) / 1000;

  return (
    <div className="stack">
      <div className="burst-controls">
        <label>
          Requests
          <input
            type="number"
            min={1}
            max={500}
            value={count}
            onChange={(event) => setCount(Math.max(1, Math.min(500, Number(event.target.value) || 1)))}
          />
        </label>
        <label>
          Pattern
          <select value={mode} onChange={(event) => setMode(event.target.value as Mode)}>
            <option value="burst">All at once</option>
            <option value="paced">Evenly paced</option>
          </select>
        </label>
        {mode === "burst" ? (
          <label>
            In flight at once
            <input
              type="number"
              min={1}
              max={50}
              value={concurrency}
              onChange={(event) => setConcurrency(Math.max(1, Math.min(50, Number(event.target.value) || 1)))}
            />
          </label>
        ) : (
          <label>
            Gap between requests (ms)
            <input
              type="number"
              min={10}
              max={5000}
              step={10}
              value={spacingMs}
              onChange={(event) => setSpacingMs(Math.max(10, Number(event.target.value) || 10))}
            />
          </label>
        )}
        {running ? (
          <button type="button" className="danger" onClick={stop}>
            Stop
          </button>
        ) : (
          <button type="button" className="primary" onClick={start} disabled={!session}>
            Fire at {route.path_prefix}
          </button>
        )}
      </div>

      {!session && (
        <div className="callout">
          These routes require a token, so the limiter counts per user.{" "}
          <Link to="/auth">Sign in first</Link>, then come back.
        </div>
      )}

      <p className="hint" style={{ margin: 0 }}>
        {mode === "burst"
          ? `${count} requests, ${concurrency} in flight at a time, against ${algorithmLabel(route.rate_limit?.algorithm ?? "")} at ${limit} per ${formatSeconds(route.rate_limit?.window_seconds ?? 0)}.`
          : `${count} requests, one every ${spacingMs} ms, over about ${pacedSeconds.toFixed(1)}s. That is ${(1000 / spacingMs).toFixed(2)}/s against an allowance of ${route.rate_limit?.requests_per_second}/s.`}{" "}
        This spends your real allowance, so other pages may see 429s on this route until it recovers.
      </p>

      {samples.length > 0 && (
        <>
          <div className="grid grid-4">
            <div className="card tile">
              <div className="tile-label">Sent</div>
              <div className="tile-value">{summary.sent}</div>
            </div>
            <div className="card tile">
              <div className="tile-label">Allowed</div>
              <div className="tile-value">{summary.allowed}</div>
            </div>
            <div className="card tile">
              <div className="tile-label">Refused, 429</div>
              <div className="tile-value">{summary.limited}</div>
            </div>
            <div className="card tile">
              <div className="tile-label">Retry-After</div>
              <div className="tile-value">
                {summary.retryAfter !== null ? `${summary.retryAfter}s` : "none"}
              </div>
            </div>
          </div>

          <BurstChart
            points={toPoints(samples)}
            yMax={limit}
            xMax={ranMode === "paced" ? pacedSeconds : undefined}
            title={`X-RateLimit-Remaining after each request to ${route.path_prefix}`}
          />

          {!running && <div className="callout">{verdict(route, samples, ranMode)}</div>}
        </>
      )}
    </div>
  );
}
