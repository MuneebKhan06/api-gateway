// Helpers for showing circuit breakers. The rules mirror
// gateway/circuit_breaker/breaker.py and store.py.

import { request, UPSTREAM_PREFIX, UPSTREAMS, type UpstreamName } from "../api/client";
import type { BreakerStatus } from "../api/types";

/**
 * The gateway names a breaker after the upstream's host, so it is
 * "service-a" under Docker Compose and "service-a.localhost" when run on a
 * host. The fault injection endpoints only know the short name.
 */
export function controlNameFor(upstream: string): UpstreamName | null {
  const short = upstream.split(".")[0];
  return (UPSTREAMS as readonly string[]).includes(short) ? (short as UpstreamName) : null;
}

/** Seconds until an open breaker lets a trial request through, or null. */
export function recoveryRemaining(breaker: BreakerStatus, now = Date.now()): number | null {
  if (breaker.state !== "open" || breaker.opened_at === null) return null;
  const elapsed = now / 1000 - breaker.opened_at;
  return Math.max(0, breaker.recovery_timeout_seconds - elapsed);
}

/**
 * /gateway/breakers reports the failure threshold and recovery timeout but
 * not these two, so they mirror the BREAKER_SUCCESS_THRESHOLD and
 * BREAKER_FAILURE_WINDOW_SECONDS defaults.
 */
export const SUCCESS_THRESHOLD = 2;
export const FAILURE_WINDOW_SECONDS = 60;

export const STATE_LABELS: Record<string, string> = {
  closed: "Closed",
  open: "Open",
  half_open: "Half open",
};

export function describeState(breaker: BreakerStatus): string {
  switch (breaker.state) {
    case "closed":
      return breaker.failures > 0
        ? `Passing traffic. ${breaker.failures} failure${breaker.failures === 1 ? "" : "s"} in a row so far; ${breaker.failure_threshold} opens it, and any success clears the count.`
        : "Passing traffic. Every request is attempted.";
    case "open":
      return `Refusing everything with 503 without calling the upstream. After ${breaker.recovery_timeout_seconds}s it lets one trial request through.`;
    case "half_open":
      return `Trying again, one request at a time. ${breaker.successes} of ${SUCCESS_THRESHOLD} successes so far closes it; any failure reopens it and restarts the timer.`;
    default:
      return "State unknown: Redis could not be read.";
  }
}

export interface UpstreamProbe {
  /** Whether the upstream itself answers its health check. */
  healthy: boolean;
  status: number;
  latencyMs: number;
}

/**
 * Ask the upstream directly, around the gateway. Its fault injection also
 * applies to /health, so this reports what the gateway will experience.
 */
export async function probeUpstream(name: UpstreamName): Promise<UpstreamProbe> {
  const result = await request(`${UPSTREAM_PREFIX}/${name}/health`);
  return {
    healthy: result.status === 200,
    status: result.status,
    latencyMs: result.durationMs,
  };
}
