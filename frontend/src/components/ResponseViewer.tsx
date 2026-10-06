import { useState } from "react";
import type { ApiResult } from "../api/types";
import { formatMs } from "../utils/format";

// Headers worth explaining, in the order they matter. Anything else is shown
// only when "all headers" is on.
const EXPLAINED: { name: string; meaning: string }[] = [
  {
    name: "x-request-id",
    meaning:
      "Set on every response, refusals included. Honoured if the client sent one, so a trace can start outside the gateway.",
  },
  {
    name: "x-ratelimit-limit",
    meaning: "Requests allowed per window for this client on this route.",
  },
  {
    name: "x-ratelimit-remaining",
    meaning: "What is left. Sent on success too, so a client can slow down before it is refused.",
  },
  {
    name: "x-ratelimit-reset",
    meaning: "Seconds until the allowance is fully restored, rounded up so 0 never lies.",
  },
  {
    name: "retry-after",
    meaning: "Sent with 429 and 503: how long to wait before trying again.",
  },
  {
    name: "x-circuit-breaker",
    meaning: "The upstream's breaker state when this request was let through or refused.",
  },
  {
    name: "x-upstream-service",
    meaning: "Set by the upstream itself, proving the request really reached it.",
  },
  {
    name: "www-authenticate",
    meaning: "Required on a 401 by RFC 9110. Tells the client to retry with a Bearer token.",
  },
];

function statusTone(status: number): string {
  if (status === 0 || status >= 500) return "bad";
  if (status >= 400) return "warn";
  return "ok";
}

function prettyBody(raw: string): string {
  if (!raw) return "(empty body)";
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
}

/**
 * service-a's /_echo/headers reports what the upstream actually received.
 * Pull out the identity headers so the stripping and injection are obvious.
 */
function IdentityCallout({ result, sent }: { result: ApiResult; sent: Record<string, string> }) {
  let received: Record<string, string> | null = null;
  try {
    const parsed = JSON.parse(result.rawBody);
    if (parsed && typeof parsed.headers === "object") received = parsed.headers;
  } catch {
    return null;
  }
  if (!received) return null;

  const identity = Object.entries(received).filter(([name]) => name.startsWith("x-gateway-user"));
  const spoofed = Object.entries(sent).filter(([name]) =>
    name.toLowerCase().startsWith("x-gateway-user"),
  );

  return (
    <div className="callout">
      <strong>What the upstream received.</strong>{" "}
      {identity.length > 0
        ? "The gateway injected the caller's identity from the verified token:"
        : "No identity headers, because the request was not authenticated."}
      {identity.length > 0 && (
        <ul className="identity-list">
          {identity.map(([name, value]) => (
            <li key={name}>
              <code>{name}</code> <code className="header-value">{value}</code>
            </li>
          ))}
        </ul>
      )}
      {spoofed.length > 0 && (
        <div>
          You sent{" "}
          {spoofed.map(([name, value]) => (
            <code key={name}>
              {name}: {value}
            </code>
          ))}{" "}
          and it was stripped, so an upstream can trust these headers came from the gateway.
        </div>
      )}
    </div>
  );
}

interface ResponseViewerProps {
  result: ApiResult;
  /** Headers the client sent, to show which ones the gateway stripped. */
  sentHeaders?: Record<string, string>;
}

export default function ResponseViewer({ result, sentHeaders = {} }: ResponseViewerProps) {
  const [showAll, setShowAll] = useState(false);
  const explainedNames = new Set(EXPLAINED.map((header) => header.name));
  const present = EXPLAINED.filter((header) => header.name in result.headers);
  const others = Object.entries(result.headers).filter(([name]) => !explainedNames.has(name));

  return (
    <div className="stack">
      <div className="row">
        <span className={`pill ${statusTone(result.status)} plain mono status-pill`}>
          {result.status || "ERR"}
        </span>
        <code>
          {result.method} {result.url.replace(/^\/gw/, "")}
        </code>
        <span className="hint" style={{ marginLeft: "auto" }}>
          {formatMs(result.durationMs)} round trip
        </span>
      </div>

      {result.url.includes("/_echo/headers") && result.ok && (
        <IdentityCallout result={result} sent={sentHeaders} />
      )}

      <div className="response-grid">
        <div className="stack">
          <div className="section-label">Body</div>
          <pre className="code response-body">{prettyBody(result.rawBody)}</pre>
        </div>

        <div className="stack">
          <div className="section-label">Gateway headers</div>
          {present.length === 0 ? (
            <div className="hint">None on this response.</div>
          ) : (
            <dl className="header-list">
              {present.map((header) => (
                <div key={header.name}>
                  <dt>
                    <code>{header.name}</code>
                    <code className="header-value">{result.headers[header.name]}</code>
                  </dt>
                  <dd>{header.meaning}</dd>
                </div>
              ))}
            </dl>
          )}

          {others.length > 0 && (
            <>
              <button
                type="button"
                className="small"
                style={{ alignSelf: "flex-start" }}
                onClick={() => setShowAll((value) => !value)}
              >
                {showAll ? "Hide" : "Show"} {others.length} other headers
              </button>
              {showAll && (
                <table className="claims">
                  <tbody>
                    {others.map(([name, value]) => (
                      <tr key={name}>
                        <td>
                          <code>{name}</code>
                        </td>
                        <td className="mono">{value}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
