// The three rate limiting algorithms, ported line for line from the Lua in
// gateway/rate_limit. Used to replay a traffic pattern against all three at
// once, which the live gateway cannot do: each route runs one algorithm, and
// some patterns (like straddling a window boundary) take minutes to set up
// for real.

export type Algorithm = "token_bucket" | "sliding_window" | "fixed_window";

export const ALGORITHMS: Algorithm[] = ["token_bucket", "sliding_window", "fixed_window"];

export interface SimulatedDecision {
  /** Seconds since the start of the pattern. */
  at: number;
  allowed: boolean;
  remaining: number;
}

export interface Limiter {
  check(now: number): SimulatedDecision;
}

/** Starts full, refills continuously at limit / window per second. */
export function tokenBucket(limit: number, windowSeconds: number): Limiter {
  const refillRate = limit / windowSeconds;
  let tokens = limit;
  let updatedAt: number | null = null;
  return {
    check(now) {
      if (updatedAt === null) updatedAt = now;
      const elapsed = Math.max(0, now - updatedAt);
      tokens = Math.min(limit, tokens + elapsed * refillRate);
      updatedAt = now;
      const allowed = tokens >= 1;
      if (allowed) tokens -= 1;
      return { at: now, allowed, remaining: Math.floor(tokens) };
    },
  };
}

/**
 * INCR plus EXPIRE: the window opens on a client's first request and lasts
 * windowSeconds from there. Rejected requests still increment the counter.
 */
export function fixedWindow(limit: number, windowSeconds: number): Limiter {
  let count = 0;
  let expiresAt = -Infinity;
  return {
    check(now) {
      if (now >= expiresAt) {
        count = 0;
        expiresAt = now + windowSeconds;
      }
      count += 1;
      return { at: now, allowed: count <= limit, remaining: Math.max(0, limit - count) };
    },
  };
}

/** A log of accepted timestamps. Rejected requests are not logged. */
export function slidingWindow(limit: number, windowSeconds: number): Limiter {
  const log: number[] = [];
  return {
    check(now) {
      const cutoff = now - windowSeconds;
      while (log.length > 0 && log[0] <= cutoff) log.shift();
      const allowed = log.length < limit;
      if (allowed) log.push(now);
      return { at: now, allowed, remaining: Math.max(0, limit - log.length) };
    },
  };
}

export function makeLimiter(algorithm: Algorithm, limit: number, windowSeconds: number): Limiter {
  switch (algorithm) {
    case "token_bucket":
      return tokenBucket(limit, windowSeconds);
    case "fixed_window":
      return fixedWindow(limit, windowSeconds);
    case "sliding_window":
      return slidingWindow(limit, windowSeconds);
  }
}

/** Replay a list of request times (seconds, ascending) through one algorithm. */
export function replay(
  algorithm: Algorithm,
  limit: number,
  windowSeconds: number,
  times: number[],
): SimulatedDecision[] {
  const limiter = makeLimiter(algorithm, limit, windowSeconds);
  return times.map((at) => limiter.check(at));
}

/** `count` requests spread evenly from `start` to `end` seconds. */
export function spread(count: number, start: number, end: number): number[] {
  if (count <= 0) return [];
  if (count === 1) return [start];
  const step = (end - start) / (count - 1);
  return Array.from({ length: count }, (_, index) => start + index * step);
}

/**
 * The most requests accepted inside any `span` second stretch. This is the
 * number the boundary problem is about: the limit says 100 per minute, so
 * any one minute should never see more than 100.
 */
export function worstWindow(decisions: SimulatedDecision[], span: number): number {
  const accepted = decisions.filter((decision) => decision.allowed).map((decision) => decision.at);
  let best = 0;
  let left = 0;
  for (let right = 0; right < accepted.length; right += 1) {
    while (accepted[right] - accepted[left] >= span) left += 1;
    best = Math.max(best, right - left + 1);
  }
  return best;
}
