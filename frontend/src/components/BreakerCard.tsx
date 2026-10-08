import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { BreakerStatus, RouteStatus } from "../api/types";
import {
  SUCCESS_THRESHOLD,
  controlNameFor,
  describeState,
  recoveryRemaining,
} from "../utils/breaker";
import BreakerDiagram from "./BreakerDiagram";
import FaultControls from "./FaultControls";
import StatusPill from "./StatusPill";

function useNow(intervalMs: number): number {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(timer);
  }, [intervalMs]);
  return now;
}

interface BreakerCardProps {
  breaker: BreakerStatus;
  routes: RouteStatus[];
  token: string | null;
  /** Called after a manual trip or reset, so the page can refetch at once. */
  onChanged: () => void;
  /** Reported up so the walkthrough can tick off the manual step. */
  onManual?: (upstream: string, action: "trip" | "reset") => void;
}

export default function BreakerCard({ breaker, routes, token, onChanged, onManual }: BreakerCardProps) {
  const now = useNow(250);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  // The breaker keeps moving after a manual action, so the confirmation
  // fades rather than sitting next to a state that has since changed.
  useEffect(() => {
    if (!message) return;
    const timer = window.setTimeout(() => setMessage(null), 4000);
    return () => window.clearTimeout(timer);
  }, [message]);

  const served = routes.filter((route) => route.upstream === breaker.url);
  const control = controlNameFor(breaker.upstream);
  const remaining = recoveryRemaining(breaker, now);

  const act = async (action: "trip" | "reset") => {
    setBusy(true);
    try {
      const result =
        action === "trip"
          ? await api.tripBreaker(breaker.upstream, token)
          : await api.resetBreaker(breaker.upstream, token);
      setMessage(result.data?.message ?? result.error?.detail ?? `HTTP ${result.status}`);
      if (result.ok) onManual?.(breaker.upstream, action);
      onChanged();
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="card breaker-card">
      <div className="card-header">
        <div>
          <h2>{breaker.upstream}</h2>
          <span>
            <code>{breaker.url}</code>, serving{" "}
            {served.map((route) => route.path_prefix).join(", ") || "no routes"}
          </span>
        </div>
        <StatusPill status={breaker.state} />
      </div>

      <div className="breaker-body">
        <BreakerDiagram
          state={breaker.state}
          failureThreshold={breaker.failure_threshold}
          recoveryTimeoutSeconds={breaker.recovery_timeout_seconds}
        />

        <div className="stack">
          <p style={{ margin: 0 }}>{describeState(breaker)}</p>

          {breaker.state === "closed" && (
            <Meter
              label="Consecutive failures"
              value={breaker.failures}
              max={breaker.failure_threshold}
              tone="bad"
            />
          )}
          {breaker.state === "open" && remaining !== null && (
            <Meter
              label={`Trial request allowed in ${Math.ceil(remaining)}s`}
              value={breaker.recovery_timeout_seconds - remaining}
              max={breaker.recovery_timeout_seconds}
              tone="warn"
            />
          )}
          {breaker.state === "half_open" && (
            <Meter
              label="Successful trials"
              value={breaker.successes}
              max={SUCCESS_THRESHOLD}
              tone="ok"
            />
          )}

          <div className="row">
            <button type="button" className="danger" disabled={busy || breaker.state === "open"} onClick={() => act("trip")}>
              Trip open
            </button>
            <button type="button" disabled={busy || breaker.state === "closed"} onClick={() => act("reset")}>
              Reset closed
            </button>
            {message && <span className="hint">{message}</span>}
          </div>
          <p className="hint" style={{ margin: 0 }}>
            Operator overrides. Trip takes an upstream out of rotation before it fails; it still
            recovers on its own after the timeout. Reset skips the wait for an upstream known to
            be fixed. State lives in Redis, so every gateway instance sees the change at once.
          </p>

          {control && (
            <FaultControls upstream={control} timeoutSeconds={served[0]?.timeout_seconds ?? null} />
          )}
        </div>
      </div>
    </section>
  );
}

function Meter({ label, value, max, tone }: { label: string; value: number; max: number; tone: string }) {
  const percent = max > 0 ? Math.min(100, (value / max) * 100) : 0;
  return (
    <div className="meter">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <span className="hint">{label}</span>
        <span className="mono hint">
          {Number.isInteger(value) ? value : value.toFixed(0)} / {max}
        </span>
      </div>
      <div className={`meter-track ${tone}`}>
        <div className="meter-fill" style={{ width: `${percent}%` }} />
      </div>
    </div>
  );
}
