// The middleware chain, in the order the gateway actually runs it
// (gateway/main.py, Decision 7 in the README). Shown on the overview to
// explain the request path, and reused by the playground to point at the
// layer that answered a request.

export type StageId =
  | "correlation"
  | "metrics"
  | "auth"
  | "rate_limit"
  | "breaker"
  | "proxy"
  | "upstream";

export interface Stage {
  id: StageId;
  name: string;
  detail: string;
  rejects?: string;
}

export const STAGES: Stage[] = [
  {
    id: "correlation",
    name: "Correlation ID",
    detail: "Tags the request with X-Request-ID so every response, even a refusal, can be traced.",
  },
  {
    id: "metrics",
    name: "Metrics",
    detail: "Starts the clock, so the recorded duration covers everything the gateway does.",
  },
  {
    id: "auth",
    name: "JWT auth",
    detail: "Signature and expiry first (CPU only), then the Redis blacklist.",
    rejects: "401",
  },
  {
    id: "rate_limit",
    name: "Rate limiter",
    detail: "One atomic Lua script in Redis, charged per user or per IP.",
    rejects: "429",
  },
  {
    id: "breaker",
    name: "Circuit breaker",
    detail: "Refuses calls to an upstream that is already failing.",
    rejects: "503",
  },
  {
    id: "proxy",
    name: "Reverse proxy",
    detail: "Longest prefix route match, then streams the request upstream.",
    rejects: "404, 502, 504",
  },
  {
    id: "upstream",
    name: "Upstream",
    detail: "The service behind the route answers, and the gateway streams it back.",
  },
];

/**
 * idle: nothing sent yet. passed: ran and let the request through.
 * stopped: refused the request. answered: produced the final response.
 * skipped: switched off for this route. unreached: an earlier stage answered.
 */
export type StageState = "idle" | "passed" | "stopped" | "answered" | "skipped" | "unreached";

interface PipelineProps {
  states?: Partial<Record<StageId, StageState>>;
  notes?: Partial<Record<StageId, string>>;
  compact?: boolean;
}

export default function Pipeline({ states = {}, notes = {}, compact = false }: PipelineProps) {
  return (
    <ol className={`pipeline${compact ? " compact" : ""}`}>
      {STAGES.map((stage, index) => {
        const state = states[stage.id] ?? "idle";
        return (
          <li key={stage.id} className={`stage ${state}`}>
            <div className="stage-index">{index + 1}</div>
            <div className="stage-body">
              <div className="stage-name">
                {stage.name}
                {stage.rejects && <span className="stage-rejects">{stage.rejects}</span>}
                {notes[stage.id] && <span className="stage-note">{notes[stage.id]}</span>}
              </div>
              {!compact && <div className="stage-detail">{stage.detail}</div>}
            </div>
          </li>
        );
      })}
    </ol>
  );
}
