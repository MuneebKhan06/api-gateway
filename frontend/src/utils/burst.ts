// Fire a burst of requests at one route and record what the rate limiter
// said about each. The limiter is the thing under test, so every sample keeps
// the X-RateLimit headers, not just the status.

import { request } from "../api/client";

export interface BurstSample {
  index: number;
  /** Milliseconds after the burst started, when this request was sent. */
  sentAt: number;
  /** Milliseconds after the burst started, when the response arrived. */
  receivedAt: number;
  status: number;
  allowed: boolean;
  limit: number | null;
  remaining: number | null;
  reset: number | null;
  retryAfter: number | null;
  error: string | null;
}

export interface BurstOptions {
  url: string;
  token: string | null;
  count: number;
  /**
   * How many requests are in flight at once. Ignored when spacingMs is set,
   * since a paced burst sends on a clock rather than as fast as it can.
   */
  concurrency: number;
  /** Fixed gap between request starts. 0 means as fast as possible. */
  spacingMs: number;
  signal?: AbortSignal;
  onSample: (sample: BurstSample) => void;
}

const sleep = (ms: number, signal?: AbortSignal) =>
  new Promise<void>((resolve) => {
    if (ms <= 0) return resolve();
    const timer = window.setTimeout(resolve, ms);
    signal?.addEventListener("abort", () => {
      window.clearTimeout(timer);
      resolve();
    });
  });

async function fire(options: BurstOptions, index: number, started: number): Promise<void> {
  const sentAt = performance.now() - started;
  let sample: BurstSample;
  try {
    const result = await request(options.url, { token: options.token, signal: options.signal });
    sample = {
      index,
      sentAt,
      receivedAt: performance.now() - started,
      status: result.status,
      allowed: result.status !== 429,
      limit: result.gateway.rateLimitLimit,
      remaining: result.gateway.rateLimitRemaining,
      reset: result.gateway.rateLimitReset,
      retryAfter: result.gateway.retryAfter,
      error: result.error?.error ?? null,
    };
  } catch {
    // Aborted mid flight. Nothing to record.
    return;
  }
  if (!options.signal?.aborted) options.onSample(sample);
}

/** Resolves once every request has answered, or the signal aborts. */
export async function runBurst(options: BurstOptions): Promise<void> {
  const started = performance.now();

  if (options.spacingMs > 0) {
    const inFlight: Promise<void>[] = [];
    for (let index = 0; index < options.count; index += 1) {
      if (options.signal?.aborted) break;
      // Schedule against the start time rather than the previous send, so
      // slow responses do not stretch the spacing.
      await sleep(started + index * options.spacingMs - performance.now(), options.signal);
      if (options.signal?.aborted) break;
      inFlight.push(fire(options, index, started));
    }
    await Promise.all(inFlight);
    return;
  }

  let next = 0;
  const worker = async () => {
    while (next < options.count && !options.signal?.aborted) {
      const index = next;
      next += 1;
      await fire(options, index, started);
    }
  };
  const workers = Math.max(1, Math.min(options.concurrency, options.count));
  await Promise.all(Array.from({ length: workers }, worker));
}

export interface BurstSummary {
  sent: number;
  allowed: number;
  limited: number;
  other: number;
  firstLimitedIndex: number | null;
  retryAfter: number | null;
  durationMs: number;
}

export function summarize(samples: BurstSample[]): BurstSummary {
  const ordered = [...samples].sort((a, b) => a.index - b.index);
  const limited = ordered.filter((sample) => sample.status === 429);
  const allowed = ordered.filter((sample) => sample.status >= 200 && sample.status < 300);
  return {
    sent: ordered.length,
    allowed: allowed.length,
    limited: limited.length,
    other: ordered.length - allowed.length - limited.length,
    firstLimitedIndex: limited.length > 0 ? limited[0].index : null,
    retryAfter: limited.length > 0 ? limited[limited.length - 1].retryAfter : null,
    durationMs: ordered.reduce((latest, sample) => Math.max(latest, sample.receivedAt), 0),
  };
}
