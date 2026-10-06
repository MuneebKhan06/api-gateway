// Work out which layer of the gateway answered a request, from the outside.
//
// The gateway does not report its internal path, but it does not need to:
// every layer that refuses a request uses its own status code and error
// code, and the route table says which layers apply to a path at all. That
// is enough to light up the pipeline accurately for any response.

import type { ApiResult, RouteStatus } from "../api/types";
import type { StageId, StageState } from "../components/Pipeline";

export interface Trace {
  states: Partial<Record<StageId, StageState>>;
  notes: Partial<Record<StageId, string>>;
  answeredBy: StageId | null;
  headline: string;
  explanation: string;
}

const AUTH_ERRORS: Record<string, string> = {
  missing_token: "No bearer token was sent, so the request never got past auth.",
  invalid_token:
    "The signature did not verify. The token was forged or tampered with, and this is counted separately from expiry so it can be alerted on.",
  token_expired: "The token's exp claim is in the past. Access tokens live for 15 minutes.",
  wrong_token_type:
    "This is a refresh token. Its signature is valid, but its typ claim says refresh, so it cannot authenticate a request.",
  token_revoked:
    "Signature and expiry are fine, but the jti is on the Redis blacklist because this token was logged out.",
};

/**
 * Longest prefix wins, and a prefix only matches on a path segment boundary,
 * so /api/orders does not match /api/ordersXYZ. Same rules as gateway/router.py.
 */
export function matchRoute(routes: RouteStatus[], path: string): RouteStatus | null {
  const bare = path.split("?")[0];
  let best: RouteStatus | null = null;
  for (const route of routes) {
    const prefix = route.path_prefix;
    const matches = prefix === "/" || bare === prefix || bare.startsWith(`${prefix}/`);
    if (matches && (!best || prefix.length > best.path_prefix.length)) best = route;
  }
  return best;
}

/** "service-a" from "http://service-a:8001", matching the gateway's own naming. */
function upstreamHost(route: RouteStatus | null): string | null {
  if (!route?.upstream) return null;
  return route.upstream.split("://", 2)[1]?.split(/[:/]/)[0] ?? route.upstream;
}

/** Stages that the route table switches off for this route. */
function applicability(route: RouteStatus | null): Partial<Record<StageId, boolean>> {
  if (!route) return { auth: false, rate_limit: false, breaker: false, upstream: false };
  return {
    auth: route.auth_required,
    rate_limit: route.rate_limit !== null,
    breaker: route.circuit_breaker_state !== "disabled",
    upstream: route.upstream !== null,
  };
}

const ORDER: StageId[] = [
  "correlation",
  "metrics",
  "auth",
  "rate_limit",
  "breaker",
  "proxy",
  "upstream",
];

export function inferTrace(result: ApiResult, route: RouteStatus | null): Trace {
  if (result.networkError || result.status === 0) {
    return {
      states: {},
      notes: {},
      answeredBy: null,
      headline: "No response",
      explanation:
        "The request never reached the gateway. Check that the stack is running and the dev proxy points at it.",
    };
  }

  const code = result.error?.error ?? null;
  const applies = applicability(route);
  const gh = result.gateway;

  // Where the request stopped, and whether it was a refusal or an answer.
  let stop: StageId;
  let refused = false;
  let headline: string;
  let explanation: string;

  if (code === "route_not_found" || code === "not_implemented" || (!route && result.status === 404)) {
    stop = "proxy";
    refused = true;
    headline = "No route matched";
    explanation =
      "Auth, rate limiting and the breaker all step aside for a path that matches no route, so an unknown URL is a 404 rather than a 401 or 429. Answering earlier would leak which paths exist.";
  } else if (code && code in AUTH_ERRORS) {
    stop = "auth";
    refused = true;
    headline = `Refused by JWT auth: ${code}`;
    explanation = AUTH_ERRORS[code];
  } else if (code === "rate_limit_exceeded") {
    stop = "rate_limit";
    refused = true;
    headline = "Refused by the rate limiter";
    explanation = `This client used its ${gh.rateLimitLimit ?? "whole"} request allowance. Retry-After says to wait ${gh.retryAfter ?? "?"}s. The upstream was never called.`;
  } else if (code === "circuit_open") {
    stop = "breaker";
    refused = true;
    headline = "Refused by the circuit breaker";
    explanation =
      "The breaker for this upstream is open, so the gateway answered 503 immediately instead of sending traffic to a service that is already failing.";
  } else if (code === "upstream_timeout") {
    stop = "proxy";
    refused = true;
    headline = "Upstream timed out";
    explanation = `The upstream did not answer within the route's ${route?.timeout_seconds ?? "configured"}s timeout, so the gateway returned 504. This counts as a breaker failure.`;
  } else if (code === "upstream_unavailable") {
    stop = "proxy";
    refused = true;
    headline = "Upstream unreachable";
    explanation =
      "The gateway could not connect to the upstream and returned 502. This counts as a breaker failure.";
  } else if (applies.upstream) {
    stop = "upstream";
    headline =
      result.status >= 500
        ? `Upstream answered ${result.status}`
        : `Proxied to ${gh.upstreamService ?? upstreamHost(route) ?? "the upstream"}`;
    explanation =
      result.status >= 500
        ? "The upstream itself failed. The gateway passed its answer through unchanged and recorded it as a breaker failure. Five of these in a minute opens the breaker."
        : result.status >= 400
          ? "The upstream refused this, not the gateway. 4xx answers never count against the breaker, so bad client requests cannot take a healthy service offline."
          : "Every layer let it through and the upstream's response was streamed back with the gateway's headers added.";
  } else {
    stop = "proxy";
    headline = "Answered by the gateway itself";
    explanation =
      "This path is owned by the gateway (auth, health, metrics or introspection), so nothing was proxied.";
  }

  const states: Partial<Record<StageId, StageState>> = {};
  let reached = true;
  for (const stage of ORDER) {
    if (!reached) {
      states[stage] = "unreached";
      continue;
    }
    if (stage === stop) {
      states[stage] = refused ? "stopped" : "answered";
      reached = false;
      continue;
    }
    // A stage the route table switches off is passed over, not run.
    states[stage] = applies[stage] === false ? "skipped" : "passed";
  }

  const notes: Partial<Record<StageId, string>> = {};
  if (gh.requestId) notes.correlation = gh.requestId.slice(0, 12);
  if (states.auth === "passed") notes.auth = "token valid";
  if (states.auth === "skipped") notes.auth = route ? "public route" : "no route";
  if (gh.rateLimitLimit !== null && states.rate_limit !== "stopped") {
    notes.rate_limit = `${gh.rateLimitRemaining} of ${gh.rateLimitLimit} left`;
  }
  if (states.rate_limit === "skipped") notes.rate_limit = "no limit on this route";
  if (gh.circuitBreaker && states.breaker === "passed") notes.breaker = `breaker ${gh.circuitBreaker}`;
  if (states.breaker === "skipped") notes.breaker = "not proxied";
  if (states.upstream === "answered" && gh.upstreamService) notes.upstream = gh.upstreamService;
  if (stop === "proxy" && !refused) notes.proxy = "served in process";

  return { states, notes, answeredBy: stop, headline, explanation };
}
