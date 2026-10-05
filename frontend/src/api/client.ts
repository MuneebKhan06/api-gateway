import type {
  ApiResult,
  BreakerStatus,
  GatewayError,
  GatewayHeaders,
  HealthResponse,
  MessageResponse,
  RouteStatus,
  TokenPair,
  UserResponse,
} from "./types";

// Same-origin prefixes, forwarded by the Vite dev server or nginx.
export const GATEWAY_PREFIX = "/gw";
export const UPSTREAM_PREFIX = "/upstream";

export const UPSTREAMS = ["service-a", "service-b", "service-c"] as const;
export type UpstreamName = (typeof UPSTREAMS)[number];

export interface RequestOptions {
  method?: string;
  body?: unknown;
  token?: string | null;
  headers?: Record<string, string>;
  signal?: AbortSignal;
}

function toNumber(value: string | null): number | null {
  if (value === null || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function readGatewayHeaders(headers: Headers): GatewayHeaders {
  return {
    requestId: headers.get("x-request-id"),
    rateLimitLimit: toNumber(headers.get("x-ratelimit-limit")),
    rateLimitRemaining: toNumber(headers.get("x-ratelimit-remaining")),
    rateLimitReset: toNumber(headers.get("x-ratelimit-reset")),
    retryAfter: toNumber(headers.get("retry-after")),
    circuitBreaker: headers.get("x-circuit-breaker"),
    upstreamService: headers.get("x-upstream-service"),
  };
}

function isGatewayError(value: unknown): value is GatewayError {
  return (
    typeof value === "object" &&
    value !== null &&
    "error" in value &&
    "detail" in value &&
    typeof (value as GatewayError).error === "string"
  );
}

const EMPTY_HEADERS: GatewayHeaders = {
  requestId: null,
  rateLimitLimit: null,
  rateLimitRemaining: null,
  rateLimitReset: null,
  retryAfter: null,
  circuitBreaker: null,
  upstreamService: null,
};

/**
 * Send one request and record everything about it.
 *
 * Never throws for an HTTP error: a 401, 429 or 503 is exactly what the
 * console wants to show, so it comes back as data. Only an abort propagates.
 */
export async function request<T = unknown>(
  url: string,
  options: RequestOptions = {},
): Promise<ApiResult<T>> {
  const method = (options.method ?? "GET").toUpperCase();
  const headers: Record<string, string> = { Accept: "application/json", ...options.headers };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (options.token) headers.Authorization = `Bearer ${options.token}`;

  const started = performance.now();
  let response: Response;
  try {
    response = await fetch(url, {
      method,
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: options.signal,
    });
  } catch (exc) {
    if ((exc as Error).name === "AbortError") throw exc;
    return {
      ok: false,
      status: 0,
      method,
      url,
      durationMs: performance.now() - started,
      data: null,
      error: { error: "network_error", detail: String(exc), request_id: null },
      rawBody: "",
      headers: {},
      gateway: EMPTY_HEADERS,
      networkError: true,
    };
  }

  const rawBody = await response.text();
  const durationMs = performance.now() - started;

  let parsed: unknown = null;
  if (rawBody) {
    try {
      parsed = JSON.parse(rawBody);
    } catch {
      parsed = null;
    }
  }

  const allHeaders: Record<string, string> = {};
  response.headers.forEach((value, key) => {
    allHeaders[key] = value;
  });

  return {
    ok: response.ok,
    status: response.status,
    method,
    url,
    durationMs,
    data: response.ok ? (parsed as T) : null,
    error: !response.ok && isGatewayError(parsed) ? parsed : null,
    rawBody,
    headers: allHeaders,
    gateway: readGatewayHeaders(response.headers),
    networkError: false,
  };
}

/** Prefix a gateway path, so callers write paths exactly as the gateway sees them. */
export function gw(path: string): string {
  return `${GATEWAY_PREFIX}${path.startsWith("/") ? path : `/${path}`}`;
}

export const api = {
  health: () => request<HealthResponse>(gw("/health")),
  routes: () => request<RouteStatus[]>(gw("/gateway/routes")),
  breakers: () => request<BreakerStatus[]>(gw("/gateway/breakers")),
  metrics: () => request<string>(gw("/metrics"), { headers: { Accept: "text/plain" } }),

  register: (email: string, password: string) =>
    request<UserResponse>(gw("/auth/register"), { method: "POST", body: { email, password } }),
  login: (email: string, password: string) =>
    request<TokenPair>(gw("/auth/login"), { method: "POST", body: { email, password } }),
  refresh: (refreshToken: string) =>
    request<TokenPair>(gw("/auth/refresh"), {
      method: "POST",
      body: { refresh_token: refreshToken },
    }),
  logout: (accessToken: string, refreshToken?: string | null) =>
    request<MessageResponse>(gw("/auth/logout"), {
      method: "POST",
      token: accessToken,
      body: refreshToken ? { refresh_token: refreshToken } : undefined,
    }),

  resetBreaker: (upstream: string, token?: string | null) =>
    request<MessageResponse>(gw(`/gateway/breakers/${upstream}/reset`), {
      method: "POST",
      token,
    }),
  tripBreaker: (upstream: string, token?: string | null) =>
    request<MessageResponse>(gw(`/gateway/breakers/${upstream}/trip`), {
      method: "POST",
      token,
    }),

  // Fault injection goes straight to the mock upstream, bypassing the gateway,
  // the same way the README drives it with curl.
  setUpstreamFailing: (upstream: UpstreamName, enabled: boolean) =>
    request<{ failing: boolean }>(
      `${UPSTREAM_PREFIX}/${upstream}/_control/fail?enabled=${enabled}`,
      { method: "POST" },
    ),
  setUpstreamLatency: (upstream: UpstreamName, milliseconds: number) =>
    request<{ latency_ms: number }>(
      `${UPSTREAM_PREFIX}/${upstream}/_control/latency?milliseconds=${milliseconds}`,
      { method: "POST" },
    ),
};
