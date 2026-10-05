import type { ApiResult } from "../api/types";
import { formatMs } from "../utils/format";

export interface LogEntry {
  id: number;
  label: string;
  result: ApiResult;
  note?: string;
}

function tone(status: number): string {
  if (status === 0 || status >= 500) return "bad";
  if (status >= 400) return "warn";
  return "ok";
}

/** A running list of requests and what the gateway said about each. */
export default function ResultLog({ entries }: { entries: LogEntry[] }) {
  if (entries.length === 0) {
    return <div className="empty">Nothing sent yet. Every request made on this page lands here.</div>;
  }

  return (
    <ol className="result-log">
      {entries.map((entry) => {
        const { result } = entry;
        return (
          <li key={entry.id}>
            <div className="row">
              <span className={`pill ${tone(result.status)} plain mono`}>
                {result.status || "ERR"}
              </span>
              <strong>{entry.label}</strong>
              <code className="hint">
                {result.method} {result.url.replace(/^\/gw/, "")}
              </code>
              <span className="hint" style={{ marginLeft: "auto" }}>
                {formatMs(result.durationMs)}
              </span>
            </div>
            {result.error && (
              <div className="hint">
                <code>{result.error.error}</code>: {result.error.detail}
              </div>
            )}
            {entry.note && <div className="log-note">{entry.note}</div>}
            {result.gateway.requestId && (
              <div className="hint mono">X-Request-ID {result.gateway.requestId}</div>
            )}
          </li>
        );
      })}
    </ol>
  );
}
