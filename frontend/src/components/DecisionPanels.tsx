import { useState } from "react";
import type { Metrics } from "../hooks/useMetrics";
import { algorithmLabel } from "../utils/format";
import { select, sumBy } from "../utils/prometheus";
import StatusPill from "./StatusPill";

// Every decision the gateway makes is counted, so each one can be graphed and
// alerted on. These panels show the counters as totals since the gateway
// started, with the last 15 seconds alongside.

const AUTH_REASONS: Record<string, string> = {
  none: "accepted",
  missing_token: "no token sent",
  token_expired: "expired, normal churn",
  invalid_token: "bad signature, worth alerting on",
  wrong_token_type: "refresh token used as access token",
  token_revoked: "logged out, the blacklist working",
};

const BREAKER_STATES = ["closed", "open", "half_open"];

interface BarRow {
  key: string;
  label: string;
  note?: string;
  total: number;
  recent: number | null;
  tone?: "ok" | "bad";
}

/** Horizontal bars from one zero baseline, totals labelled at the tip. */
function Bars({ rows, empty }: { rows: BarRow[]; empty: string }) {
  const max = Math.max(1, ...rows.map((row) => row.total));
  if (rows.length === 0) return <div className="hint">{empty}</div>;
  return (
    <div className="stack" style={{ gap: 8 }}>
      {rows.map((row) => (
        <div key={row.key} className="decision-row" title={`${row.label}: ${row.total} total`}>
          <div className="decision-label">
            <span>{row.label}</span>
            {row.note && <span className="hint">{row.note}</span>}
          </div>
          <span className="bench-track">
            <span
              className={`bench-bar${row.tone ? ` ${row.tone}` : ""}`}
              style={{ width: `${(row.total / max) * 100}%` }}
            />
          </span>
          <span className="decision-value mono">
            {row.total}
            {row.recent !== null && row.recent > 0 && <span className="hint"> +{row.recent.toFixed(1)}/s</span>}
          </span>
        </div>
      ))}
    </div>
  );
}

export default function DecisionPanels({ metrics }: { metrics: Metrics }) {
  const { latest } = metrics;
  const [filter, setFilter] = useState("");
  if (!latest) return null;

  const authRows: BarRow[] = Object.entries(sumBy(latest, "gateway_auth_attempts_total", "reason"))
    .sort((a, b) => b[1] - a[1])
    .map(([reason, total]) => ({
      key: reason,
      label: reason === "none" ? "accepted" : reason,
      note: reason === "none" ? undefined : AUTH_REASONS[reason],
      total,
      recent: metrics.rate("gateway_auth_attempts_total", { reason }),
      tone: reason === "none" ? "ok" : "bad",
    }));

  const limitSamples = select(latest, "gateway_rate_limit_decisions_total");
  const limitRows: BarRow[] = limitSamples
    .map((sample) => ({
      key: `${sample.labels.route}|${sample.labels.decision}`,
      label: `${sample.labels.route} ${sample.labels.decision}`,
      note: algorithmLabel(sample.labels.algorithm),
      total: sample.value,
      recent: metrics.rate("gateway_rate_limit_decisions_total", {
        route: sample.labels.route,
        decision: sample.labels.decision,
      }),
      tone: (sample.labels.decision === "allowed" ? "ok" : "bad") as "ok" | "bad",
    }))
    .sort((a, b) => a.key.localeCompare(b.key));

  const breakerState = sumBy(latest, "gateway_circuit_breaker_state", "upstream");
  const transitions = select(latest, "gateway_circuit_breaker_transitions_total");
  const rejections = sumBy(latest, "gateway_circuit_breaker_rejections_total", "upstream");
  const degraded = select(latest, "gateway_degraded_operations_total");
  const dependencies = sumBy(latest, "gateway_dependency_health", "dependency");

  const families = Object.keys(latest.types)
    .filter((name) => name.startsWith("gateway_") && !name.endsWith("_created"))
    .filter((name) => name.includes(filter.trim()))
    .sort();

  return (
    <>
      <div className="grid grid-2">
        <section className="card">
          <div className="card-header">
            <h2>Authentication</h2>
            <span>gateway_auth_attempts_total by reason</span>
          </div>
          <Bars rows={authRows} empty="No authenticated routes called yet." />
          <p className="hint" style={{ marginBottom: 0 }}>
            The reason label keeps an expired token, a forged one and a revoked one apart. The
            first is normal, the second is an attack, the third means logout works.
          </p>
        </section>

        <section className="card">
          <div className="card-header">
            <h2>Rate limiting</h2>
            <span>gateway_rate_limit_decisions_total</span>
          </div>
          <Bars rows={limitRows} empty="No rate limited routes called yet." />
          <p className="hint" style={{ marginBottom: 0 }}>
            Labelled by route and algorithm, never by client: one series per client would grow
            without bound. The question this answers is which routes are throttling.
          </p>
        </section>
      </div>

      <div className="grid grid-2">
        <section className="card">
          <div className="card-header">
            <h2>Circuit breakers</h2>
            <span>state gauge, transitions and refusals</span>
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Upstream</th>
                  <th>State</th>
                  <th>Transitions</th>
                  <th>Refused</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(breakerState).map(([upstream, value]) => (
                  <tr key={upstream}>
                    <td>{upstream}</td>
                    <td>
                      <StatusPill status={BREAKER_STATES[value] ?? "unknown"} />
                    </td>
                    <td className="mono">
                      {transitions
                        .filter((sample) => sample.labels.upstream === upstream)
                        .map((sample) => `${sample.value} to ${sample.labels.to_state.replace("_", " ")}`)
                        .join(", ") || "none"}
                    </td>
                    <td className="mono">{rejections[upstream] ?? 0}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {Object.keys(breakerState).length === 0 && (
              <div className="hint">No breaker has been consulted yet.</div>
            )}
          </div>
          <p className="hint" style={{ marginBottom: 0 }}>
            The gauge shows where a breaker is; the transition counter is what alerting wants. A
            breaker flapping open and closed looks calm on a gauge scraped every 15s and obvious
            on a counter.
          </p>
        </section>

        <section className="card">
          <div className="card-header">
            <h2>Failing open</h2>
            <span>gateway_degraded_operations_total</span>
          </div>
          <div className="row" style={{ marginBottom: 10 }}>
            {Object.entries(dependencies).map(([dependency, value]) => (
              <StatusPill key={dependency} status={value === 1 ? "connected" : "unavailable"} label={`${dependency} ${value === 1 ? "up" : "down"}`} />
            ))}
          </div>
          {degraded.length === 0 ? (
            <div className="callout">
              Zero so far. If Redis goes away, the blacklist, the rate limiters and breaker checks
              let requests through instead of failing everything, and each one is counted here so
              it can be alerted on rather than found in logs later.
            </div>
          ) : (
            <Bars
              rows={degraded.map((sample) => ({
                key: `${sample.labels.component}|${sample.labels.reason}`,
                label: sample.labels.component,
                note: sample.labels.reason,
                total: sample.value,
                recent: metrics.rate("gateway_degraded_operations_total", { component: sample.labels.component }),
                tone: "bad",
              }))}
              empty=""
            />
          )}
        </section>
      </div>

      <section className="card">
        <div className="card-header">
          <h2>Every metric</h2>
          <span>{families.length} gateway families in this scrape</span>
        </div>
        <input
          className="mono"
          placeholder="filter, e.g. breaker"
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
          aria-label="Filter metric names"
        />
        <div className="table-wrap" style={{ marginTop: 10, maxHeight: 320, overflowY: "auto" }}>
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Type</th>
                <th>Series</th>
                <th>Meaning</th>
              </tr>
            </thead>
            <tbody>
              {families.map((name) => {
                const type = latest.types[name];
                const series =
                  type === "histogram"
                    ? select(latest, `${name}_count`).length
                    : select(latest, name).length;
                return (
                  <tr key={name}>
                    <td>
                      <code>{name}</code>
                    </td>
                    <td>{type}</td>
                    <td className="mono">{series}</td>
                    <td className="hint">{latest.help[name]}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>
    </>
  );
}
