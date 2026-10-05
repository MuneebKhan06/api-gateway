import { api } from "../api/client";
import type { RouteStatus } from "../api/types";
import Pipeline from "../components/Pipeline";
import StatusPill from "../components/StatusPill";
import { usePolling } from "../hooks/usePolling";
import { algorithmLabel, formatSeconds, timeAgo } from "../utils/format";

function upstreamName(url: string | null): string | null {
  if (!url) return null;
  return url.split("://", 2)[1]?.split(":")[0]?.split("/")[0] ?? url;
}

function OfflineNotice() {
  return (
    <div className="callout bad">
      <strong>The gateway is not answering.</strong> Start the stack from the repository root and
      apply the migrations, then this page fills in on its own:
      <pre className="code" style={{ marginTop: 8 }}>
        {"docker compose up -d\ndocker compose run --rm gateway alembic upgrade head"}
      </pre>
    </div>
  );
}

function Tile({ label, children, hint }: { label: string; children: React.ReactNode; hint: string }) {
  return (
    <div className="card tile">
      <div className="tile-label">{label}</div>
      <div className="tile-value">{children}</div>
      <div className="hint">{hint}</div>
    </div>
  );
}

function RouteRow({ route }: { route: RouteStatus }) {
  const limit = route.rate_limit;
  return (
    <tr>
      <td>
        <code>{route.path_prefix}</code>
        {route.strip_prefix && route.upstream && <div className="hint">prefix stripped</div>}
      </td>
      <td>
        {route.upstream ? (
          <>
            <code>{route.upstream}</code>
            <div className="hint">timeout {route.timeout_seconds}s</div>
          </>
        ) : (
          <span className="hint">served by the gateway</span>
        )}
      </td>
      <td>
        {route.auth_required ? (
          <span className="pill info plain">JWT required</span>
        ) : (
          <span className="pill plain">public</span>
        )}
      </td>
      <td>
        {limit ? (
          <>
            {algorithmLabel(limit.algorithm)}
            <div className="hint">
              {limit.requests} per {formatSeconds(limit.window_seconds)} (
              {limit.requests_per_second}/s)
            </div>
          </>
        ) : (
          <span className="hint">none</span>
        )}
      </td>
      <td>
        <StatusPill status={route.circuit_breaker_state} />
      </td>
    </tr>
  );
}

export default function Overview() {
  const health = usePolling(api.health, 5000);
  const routes = usePolling(api.routes, 5000);

  const offline = health.result?.networkError || (health.result && health.result.status >= 500 && !health.result.data);
  const data = health.result?.data;
  const routeList = routes.result?.data ?? [];

  return (
    <>
      <div className="page-header">
        <h1>Overview</h1>
        <p>
          A FastAPI gateway built from scratch: JWT auth, Redis backed rate limiting with three
          algorithms, a shared circuit breaker and Prometheus metrics. Everything on this page is
          read live from <code>/health</code> and <code>/gateway/routes</code>.
        </p>
      </div>

      {offline && <OfflineNotice />}

      <div className="grid grid-4">
        <Tile label="Gateway" hint={`checked ${timeAgo(health.updatedAt)}`}>
          <StatusPill status={offline ? "offline" : (data?.status ?? "unknown")} />
        </Tile>
        <Tile label="Routes loaded" hint="from routes.yaml, reloadable with SIGHUP">
          {data?.routes_loaded ?? "..."}
        </Tile>
        <Tile label="Redis" hint="rate limits, blacklist, breaker state">
          <StatusPill status={data?.redis ?? "unknown"} />
        </Tile>
        <Tile label="Postgres" hint="users, refresh tokens, audit log">
          <StatusPill status={data?.database ?? "unknown"} />
        </Tile>
      </div>

      <div className="grid grid-2">
        <section className="card">
          <div className="card-header">
            <h2>Request path</h2>
            <span>outermost first</span>
          </div>
          <Pipeline />
        </section>

        <section className="card">
          <div className="card-header">
            <h2>Upstream services</h2>
            <span>probed concurrently on every health check</span>
          </div>
          {data && Object.keys(data.upstreams).length > 0 ? (
            <div className="stack">
              {Object.entries(data.upstreams).map(([name, status]) => {
                const served = routeList.filter((route) => upstreamName(route.upstream) === name);
                return (
                  <div key={name} className="upstream-row">
                    <div>
                      <strong>{name}</strong>
                      <div className="hint">
                        {served.map((route) => route.path_prefix).join(", ") || "no routes"}
                      </div>
                    </div>
                    <StatusPill status={status} />
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="empty">No upstream data yet.</div>
          )}
          <p className="hint" style={{ marginBottom: 0 }}>
            An upstream whose breaker is open is reported straight from the breaker rather than
            probed again, so a struggling service is not hit harder by its own health checks.
          </p>
        </section>
      </div>

      <section className="card">
        <div className="card-header">
          <h2>Route table</h2>
          <span>as the gateway is enforcing it right now, longest prefix wins</span>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Prefix</th>
                <th>Upstream</th>
                <th>Auth</th>
                <th>Rate limit</th>
                <th>Breaker</th>
              </tr>
            </thead>
            <tbody>
              {routeList.map((route) => (
                <RouteRow key={route.path_prefix} route={route} />
              ))}
            </tbody>
          </table>
          {routeList.length === 0 && <div className="empty">No routes reported.</div>}
        </div>
      </section>
    </>
  );
}
