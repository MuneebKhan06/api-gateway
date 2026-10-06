import type { ApiResult } from "../api/types";
import type { DraftRequest } from "./RequestBuilder";
import { formatMs } from "../utils/format";

export interface HistoryEntry {
  id: number;
  draft: DraftRequest;
  result: ApiResult;
  headline: string;
  sentAt: number;
}

function tone(status: number): string {
  if (status === 0 || status >= 500) return "bad";
  if (status >= 400) return "warn";
  return "ok";
}

interface RequestHistoryProps {
  entries: HistoryEntry[];
  selectedId: number | null;
  onSelect: (entry: HistoryEntry) => void;
  onClear: () => void;
}

export default function RequestHistory({ entries, selectedId, onSelect, onClear }: RequestHistoryProps) {
  return (
    <section className="card">
      <div className="card-header">
        <h2>History</h2>
        {entries.length > 0 ? (
          <button type="button" className="small" onClick={onClear}>
            Clear
          </button>
        ) : (
          <span>this tab only</span>
        )}
      </div>
      {entries.length === 0 ? (
        <div className="hint">Requests you send are kept here so you can compare them.</div>
      ) : (
        <ol className="history">
          {entries.map((entry) => (
            <li key={entry.id}>
              <button
                type="button"
                className={`history-item${entry.id === selectedId ? " selected" : ""}`}
                onClick={() => onSelect(entry)}
              >
                <span className={`pill ${tone(entry.result.status)} plain mono`}>
                  {entry.result.status || "ERR"}
                </span>
                <span className="history-text">
                  <code>
                    {entry.draft.method} {entry.draft.path}
                  </code>
                  <span className="hint">{entry.headline}</span>
                </span>
                <span className="hint">{formatMs(entry.result.durationMs)}</span>
              </button>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
