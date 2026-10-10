import { gw } from "../api/client";
import DecisionPanels from "../components/DecisionPanels";
import Sparkline from "../components/Sparkline";
import StatusPill from "../components/StatusPill";
import { RATE_WINDOW_MS, SCRAPE_INTERVAL_MS, useMetrics, type Metrics } from "../hooks/useMetrics";
import { formatMs } from "../utils/format";
import { sum, sumBy, type LabelFilter } from "../utils/prometheus";

const GRAFANA_URL = "http://localhost:3000/d/api-gateway";
const PROMETHEUS_URL = `http://localhost:9090/graph?g0.expr=${encodeURIComponent(
  "sum by (upstream) (rate(gateway_requests_total[1m]))",
)}&g0.tab=0`;

// Labels that are not an upstream: "gateway" for what it serves itself, and
// "unmatched" for paths that match no route, which the gateway collapses into
// one series so arbitrary URLs cannot create unbounded metrics.
const NOT_PROXIED: Record<string, { label: string; note: string }> = {
  gateway: { label: "gateway itself", note: "auth, health, metrics" },
  unmatched: { label: "no route matched", note: "unknown paths and introspection, one series" },
};

const PROXIED: LabelFilter = { upstream: (value) => !(value in NOT_PROXIED) };

function perSecond(value: number | null): string {
  if (value === null) return "...";
  return value >= 10 ? value.toFixed(0) : value.toFixed(2);
}

function ms(seconds: number | null): string {
  return seconds === null ? "..." : formatMs(seconds * 1000);
}

/**
 * Average of a histogram over the rate window: increase of _sum divided by
 * increase of _count. Falls back to all time when nothing happened recently.
 */
function average(metrics: Metrics, histogram: string, filter: LabelFilter): number | null {
  const count = metrics.rate(`${histogram}_count`, filter);
  const total = metrics.rate(`${histogram}_sum`, filter);
  if (count && total !== null && count > 0) return total / count;
  if (!metrics.latest) return null;
  const allCount = sum(metrics.latest, `${histogram}_count`, filter);
  return allCount > 0 ? sum(metrics.latest, `${histogram}_sum`, filter) / allCount : null;
}

function Tile({ label, value, hint, children }: { label: string; value: string; hint: string; children?: React.ReactNode }) {
  return (
    <div className="card tile metric-tile">
      <div className="tile-label">{label}</div>
      <div className="tile-value">{value}</div>
      {children}
      <div className="hint">{hint}</div>
    </div>
  );
}

export default function MetricsPage() {
  const metrics = useMetrics();
  const { latest } = metrics;

  const requestRate = metrics.rate("gateway_requests_total");
  const errorRate = metrics.rate("gateway_errors_total");
  const errorRatio = requestRate && errorRate !== null && requestRate > 0 ? errorRate / requestRate : null;
  const p95 = metrics.quantile(0.95, "gateway_request_duration_seconds", PROXIED);
  const endToEnd = average(metrics, "gateway_request_duration_seconds", PROXIED);
  const upstreamTime = average(metrics, "gateway_upstream_duration_seconds", PROXIED);
  const overhead = endToEnd !== null && upstreamTime !== null ? Math.max(0, endToEnd - upstreamTime) : null;

  const upstreams = latest
    ? Object.keys(sumBy(latest, "gateway_requests_total", "upstream")).sort(
        (a, b) => Number(a in NOT_PROXIED) - Number(b in NOT_PROXIED) || a.localeCompare(b),
      )
    : [];
  const health = latest ? sumBy(latest, "gateway_upstream_health", "upstream") : {};
  const inFlight = latest ? sumBy(latest, "gateway_requests_in_flight", "upstream") : {};

  return (
    <>
      <div className="page-header">
        <h1>Metrics</h1>
        <p>
          The gateway exports RED metrics (rate, errors, duration) per upstream, plus a count of
          every decision it makes. This page scrapes <code>/metrics</code> every{" "}
          {SCRAPE_INTERVAL_MS / 1000}s and computes rates over the last {RATE_WINDOW_MS / 1000}s,
          the way Prometheus would. Send some traffic from the other pages and watch it move.
        </p>
      </div>

      {metrics.error && <div className="callout bad">{metrics.error}</div>}

      <div className="grid grid-4">
        <Tile label="Requests" value={`${perSecond(requestRate)}/s`} hint="everything the gateway answered">
          <Sparkline values={metrics.rateSeries("gateway_requests_total")} label="Requests per second" format={(v) => `${v.toFixed(2)}/s`} />
        </Tile>
        <Tile
          label="Error ratio"
          value={errorRatio === null ? "..." : `${(errorRatio * 100).toFixed(1)}%`}
          hint="includes deliberate refusals: 401, 429 and open breakers"
        >
          <Sparkline values={metrics.rateSeries("gateway_errors_total")} label="Errors per second" format={(v) => `${v.toFixed(2)}/s`} />
        </Tile>
        <Tile
          label="P95, proxied"
          value={ms(p95.value)}
          hint={p95.recent ? "end to end, last 15s, from histogram buckets" : "end to end, all time (nothing recent)"}
        />
        <Tile
          label="Gateway overhead"
          value={ms(overhead)}
          hint="average end to end minus average time waiting on the upstream"
        />
      </div>

      <section className="card">
        <div className="card-header">
          <h2>Per upstream</h2>
          <span>labels come from the matched route, never the raw path, so series stay bounded</span>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Upstream</th>
                <th>Requests/s</th>
                <th>Errors/s</th>
                <th>P95</th>
                <th>Upstream avg</th>
                <th>Overhead avg</th>
                <th>In flight</th>
                <th>Health</th>
              </tr>
            </thead>
            <tbody>
              {upstreams.map((upstream) => {
                const filter: LabelFilter = { upstream };
                const own = NOT_PROXIED[upstream];
                const total = average(metrics, "gateway_request_duration_seconds", filter);
                const waiting = average(metrics, "gateway_upstream_duration_seconds", filter);
                return (
                  <tr key={upstream}>
                    <td>
                      <strong>{own ? own.label : upstream}</strong>
                      {own && <div className="hint">{own.note}</div>}
                    </td>
                    <td className="mono">{perSecond(metrics.rate("gateway_requests_total", filter))}</td>
                    <td className="mono">{perSecond(metrics.rate("gateway_errors_total", filter))}</td>
                    <td className="mono">{ms(metrics.quantile(0.95, "gateway_request_duration_seconds", filter).value)}</td>
                    {own ? (
                      <>
                        <td className="hint">n/a</td>
                        <td className="hint">n/a</td>
                      </>
                    ) : (
                      <>
                        <td className="mono">{ms(waiting)}</td>
                        <td className="mono">{ms(total !== null && waiting !== null ? Math.max(0, total - waiting) : null)}</td>
                      </>
                    )}
                    <td className="mono">{inFlight[upstream] ?? 0}</td>
                    <td>
                      {own ? (
                        <span className="hint">n/a</span>
                      ) : (
                        <StatusPill status={health[upstream] === 1 ? "healthy" : health[upstream] === 0 ? "unhealthy" : "unknown"} />
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {upstreams.length === 0 && <div className="empty">No requests recorded yet.</div>}
        </div>
        <p className="hint" style={{ marginBottom: 0 }}>
          Two histograms, not one: end to end time and time spent waiting on the upstream are
          recorded separately, so a slow upstream and a slow gateway can be told apart. Health
          comes from <code>gateway_upstream_health</code>, set each time <code>/health</code> runs.
        </p>
      </section>

      <DecisionPanels metrics={metrics} />

      <section className="card">
        <div className="card-header">
          <h2>The full stack</h2>
          <span>with docker compose up</span>
        </div>
        <div className="grid grid-3">
          <a className="link-card" href={GRAFANA_URL} target="_blank" rel="noreferrer">
            <strong>Grafana dashboard</strong>
            <span className="hint">Pre-provisioned, anonymous viewing, on port 3000. Every panel query is checked by a test against the registered metrics.</span>
          </a>
          <a className="link-card" href={PROMETHEUS_URL} target="_blank" rel="noreferrer">
            <strong>Prometheus</strong>
            <span className="hint">Scrapes the gateway and evaluates the alert rules, on port 9090. Opens with requests per second by upstream.</span>
          </a>
          <a className="link-card" href={gw("/metrics")} target="_blank" rel="noreferrer">
            <strong>Raw /metrics</strong>
            <span className="hint">The exposition this page parses, exactly as Prometheus sees it.</span>
          </a>
        </div>
      </section>
    </>
  );
}
