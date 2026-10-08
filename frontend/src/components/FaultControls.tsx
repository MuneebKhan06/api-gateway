import { useCallback, useState } from "react";
import { api, type UpstreamName } from "../api/client";
import { usePolling } from "../hooks/usePolling";
import { probeUpstream } from "../utils/breaker";
import { formatMs } from "../utils/format";

interface FaultControlsProps {
  upstream: UpstreamName;
  /** The route's proxy timeout, so one latency option can exceed it. */
  timeoutSeconds: number | null;
}

/**
 * Break an upstream on purpose. These calls go straight to the mock service's
 * /_control endpoints, around the gateway, which is how the README drives the
 * breaker with curl: no container has to be stopped.
 */
export default function FaultControls({ upstream, timeoutSeconds }: FaultControlsProps) {
  const [latency, setLatency] = useState(0);
  const [busy, setBusy] = useState(false);

  const fetchProbe = useCallback(() => probeUpstream(upstream), [upstream]);
  const probe = usePolling(fetchProbe, 2000);
  const current = probe.result;
  const failing = current ? !current.healthy : false;

  const setFailing = async (enabled: boolean) => {
    setBusy(true);
    try {
      await api.setUpstreamFailing(upstream, enabled);
      await probe.refresh();
    } finally {
      setBusy(false);
    }
  };

  const changeLatency = async (milliseconds: number) => {
    setBusy(true);
    try {
      await api.setUpstreamLatency(upstream, milliseconds);
      setLatency(milliseconds);
    } finally {
      setBusy(false);
    }
    probe.refresh();
  };

  const overTimeout = timeoutSeconds !== null ? (timeoutSeconds + 2) * 1000 : null;

  return (
    <div className="fault-controls">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <span className="section-label">Upstream itself</span>
        {current ? (
          current.status === 0 ? (
            <span className="pill bad">not reachable</span>
          ) : (
            <span className={`pill ${current.healthy ? "ok" : "bad"}`}>
              {current.healthy ? "answering 200" : `answering ${current.status}`},{" "}
              {formatMs(current.latencyMs)}
            </span>
          )
        ) : (
          <span className="pill">probing</span>
        )}
      </div>

      <div className="row">
        <button
          type="button"
          className={failing ? "primary" : "danger"}
          disabled={busy}
          onClick={() => setFailing(!failing)}
        >
          {failing ? "Heal upstream" : "Make it fail (503)"}
        </button>
        <label className="inline-label">
          Added latency
          <select value={latency} disabled={busy} onChange={(event) => changeLatency(Number(event.target.value))}>
            <option value={0}>none</option>
            <option value={300}>300 ms</option>
            <option value={2000}>2 s</option>
            {overTimeout !== null && (
              <option value={overTimeout}>
                {overTimeout / 1000} s, past the {timeoutSeconds}s route timeout
              </option>
            )}
          </select>
        </label>
      </div>
      {latency === overTimeout && (
        <div className="hint">
          Each proxied call now waits out the {timeoutSeconds}s timeout and comes back 504, which
          the breaker counts as a failure.
        </div>
      )}
    </div>
  );
}
