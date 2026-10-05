// Shapes returned by the gateway, mirrored from gateway/schemas.

export type ServiceStatus = "healthy" | "unhealthy" | "unreachable" | string;

export interface HealthResponse {
  status: "healthy" | "degraded" | "unhealthy" | string;
  routes_loaded: number;
  redis: string;
  database: string;
  upstreams: Record<string, ServiceStatus>;
}

export interface RateLimitStatus {
  algorithm: "token_bucket" | "sliding_window" | "fixed_window" | string;
  requests: number;
  window_seconds: number;
  requests_per_second: number;
}

export interface RouteStatus {
  path_prefix: string;
  upstream: string | null;
  strip_prefix: boolean;
  timeout_seconds: number;
  auth_required: boolean;
  circuit_breaker_state: "closed" | "open" | "half_open" | "disabled" | "unknown" | string;
  rate_limit: RateLimitStatus | null;
}

export interface BreakerStatus {
  upstream: string;
  url: string;
  state: "closed" | "open" | "half_open" | string;
  failures: number;
  successes: number;
  opened_at: number | null;
  recovery_timeout_seconds: number;
  failure_threshold: number;
}

export interface TokenPair {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
}

export interface UserResponse {
  id: string;
  email: string;
  roles: string[];
}

export interface MessageResponse {
  message: string;
}

/** The envelope the gateway uses when it is the one refusing a request. */
export interface GatewayError {
  error: string;
  detail: string;
  request_id: string | null;
}

/** Headers the gateway adds, pulled out so pages can explain them. */
export interface GatewayHeaders {
  requestId: string | null;
  rateLimitLimit: number | null;
  rateLimitRemaining: number | null;
  rateLimitReset: number | null;
  retryAfter: number | null;
  circuitBreaker: string | null;
  upstreamService: string | null;
}

/** Everything observed about one round trip, successful or not. */
export interface ApiResult<T = unknown> {
  ok: boolean;
  status: number;
  method: string;
  url: string;
  durationMs: number;
  data: T | null;
  error: GatewayError | null;
  rawBody: string;
  headers: Record<string, string>;
  gateway: GatewayHeaders;
  /** True when the request never got an HTTP response at all. */
  networkError: boolean;
}
