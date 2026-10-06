import { useState } from "react";
import { api, gw, request } from "../api/client";
import { tamperToken } from "../api/jwt";
import type { ApiResult } from "../api/types";
import Pipeline from "../components/Pipeline";
import RequestBuilder, {
  PRESETS,
  parseHeaderLines,
  type DraftRequest,
} from "../components/RequestBuilder";
import ResponseViewer from "../components/ResponseViewer";
import { usePolling } from "../hooks/usePolling";
import { useSession, type Session } from "../session";
import { inferTrace, matchRoute, type Trace } from "../utils/trace";

function tokenFor(draft: DraftRequest, session: Session | null): string | null {
  if (!session) return null;
  switch (draft.auth) {
    case "session":
      return session.accessToken;
    case "tampered":
      return tamperToken(session.accessToken);
    case "refresh":
      return session.refreshToken;
    default:
      return null;
  }
}

/** Red for a refusal or a failure, amber for a client error the upstream gave. */
function headlineTone(trace: Trace, result: ApiResult): string {
  if (Object.values(trace.states).includes("stopped") || result.status >= 500) return "bad";
  if (result.status >= 400) return "warn";
  return "ok";
}

export default function Playground() {
  const { session } = useSession();
  const routes = usePolling(api.routes, 10000);
  const [draft, setDraft] = useState<DraftRequest>(PRESETS[0].draft);
  const [busy, setBusy] = useState(false);
  const [bodyError, setBodyError] = useState<string | null>(null);
  const [last, setLast] = useState<{ draft: DraftRequest; result: ApiResult } | null>(null);

  const send = async () => {
    let body: unknown = undefined;
    const hasBody = draft.method !== "GET" && draft.method !== "DELETE";
    if (hasBody && draft.body.trim()) {
      try {
        body = JSON.parse(draft.body);
      } catch (exc) {
        setBodyError(`The body is not valid JSON: ${(exc as Error).message}`);
        return;
      }
    }
    setBodyError(null);
    setBusy(true);
    try {
      const result = await request(gw(draft.path), {
        method: draft.method,
        token: tokenFor(draft, session),
        headers: parseHeaderLines(draft.headers),
        body,
      });
      setLast({ draft, result });
    } finally {
      setBusy(false);
    }
  };

  const routeList = routes.result?.data ?? [];
  const trace = last ? inferTrace(last.result, matchRoute(routeList, last.draft.path)) : null;
  const matched = matchRoute(routeList, draft.path);

  return (
    <>
      <div className="page-header">
        <h1>Request playground</h1>
        <p>
          Send any request through the gateway and see which layer answered it. Pick a preset to
          make a specific middleware step in: a missing or tampered token, an unknown path, the
          identity headers the upstream receives.
        </p>
      </div>

      <section className="card">
        <div className="card-header">
          <h2>Request</h2>
          <span>
            {matched ? (
              <>
                matches route <code>{matched.path_prefix}</code>
                {matched.upstream ? (
                  <>
                    {" "}
                    to <code>{matched.upstream}</code>
                  </>
                ) : (
                  " served by the gateway"
                )}
              </>
            ) : (
              "matches no route"
            )}
          </span>
        </div>
        <RequestBuilder
          draft={draft}
          onChange={setDraft}
          onSend={send}
          busy={busy}
          signedIn={session !== null}
        />
        {bodyError && (
          <div className="callout bad" style={{ marginTop: 12 }}>
            {bodyError}
          </div>
        )}
      </section>

      {last && trace ? (
        <div className="playground-result">
          <section className="card">
            <div className="card-header">
              <h2>Path through the gateway</h2>
            </div>
            <div className={`trace-headline ${headlineTone(trace, last.result)}`}>{trace.headline}</div>
            <p className="hint" style={{ marginTop: 4 }}>
              {trace.explanation}
            </p>
            <Pipeline states={trace.states} notes={trace.notes} compact />
          </section>

          <section className="card">
            <div className="card-header">
              <h2>Response</h2>
            </div>
            <ResponseViewer result={last.result} sentHeaders={parseHeaderLines(last.draft.headers)} />
          </section>
        </div>
      ) : (
        <div className="card empty">
          Send a request and its path through the middleware chain shows up here.
        </div>
      )}
    </>
  );
}
