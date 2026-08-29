#!/usr/bin/env python
"""Compare the three rate limiting algorithms.

The ADR claims token bucket is the right default, that fixed window has a
boundary problem, and that sliding window costs memory proportional to traffic.
Those are testable claims, and this measures them rather than asserting them.

    docker compose -f docker-compose.test.yml up -d
    python scripts/benchmark_algorithms.py --requests 10000 --concurrency 50

What it measures, per algorithm:

- throughput, requests per second the limiter itself can decide on
- latency, average and p95 for a single decision
- memory, bytes Redis holds per client

Requires a real Redis. Benchmarking against fakeredis would measure Python,
not Redis, and the Lua execution is most of what is being compared.
"""

import argparse
import asyncio
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import redis.asyncio as redis

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway.rate_limit.base import BaseRateLimiter  # noqa: E402
from gateway.rate_limit.fixed_window import FixedWindowLimiter  # noqa: E402
from gateway.rate_limit.sliding_window import SlidingWindowLimiter  # noqa: E402
from gateway.rate_limit.token_bucket import TokenBucketLimiter  # noqa: E402

ALGORITHMS = {
    "fixed_window": FixedWindowLimiter,
    "token_bucket": TokenBucketLimiter,
    "sliding_window": SlidingWindowLimiter,
}

ROUTE = "/benchmark"


@dataclass
class Result:
    algorithm: str
    requests: int
    concurrency: int
    duration_seconds: float
    latencies_ms: list[float]
    allowed: int
    rejected: int
    bytes_per_client: float

    @property
    def throughput(self) -> float:
        return self.requests / self.duration_seconds

    @property
    def avg_latency_ms(self) -> float:
        return statistics.mean(self.latencies_ms)

    @property
    def p95_latency_ms(self) -> float:
        ordered = sorted(self.latencies_ms)
        return ordered[int(len(ordered) * 0.95)]

    @property
    def p99_latency_ms(self) -> float:
        ordered = sorted(self.latencies_ms)
        return ordered[int(len(ordered) * 0.99)]


async def measure_memory(client: redis.Redis, limiter: BaseRateLimiter, clients: int) -> float:
    """Bytes Redis holds per client key, via MEMORY USAGE.

    Averaged over several clients, because a sorted set's overhead varies with
    how many entries it happens to hold.
    """
    total = 0
    counted = 0
    for i in range(min(clients, 50)):
        key = limiter._key(f"client-{i}", ROUTE)
        usage = await client.memory_usage(key)
        if usage:
            total += usage
            counted += 1
    return total / counted if counted else 0.0


async def run_one(
    client: redis.Redis,
    name: str,
    requests: int,
    concurrency: int,
    clients: int,
    limit: int,
    window: int,
) -> Result:
    limiter_class = ALGORITHMS[name]
    limiter = limiter_class(client, limit=limit, window_seconds=window)

    # Every algorithm starts from a clean slate, or whichever ran first would
    # leave counters that change the next one's answer.
    async for key in client.scan_iter(match="ratelimit:*", count=1000):
        await client.delete(key)

    latencies: list[float] = []
    allowed = 0
    rejected = 0
    semaphore = asyncio.Semaphore(concurrency)

    async def one_request(index: int) -> None:
        nonlocal allowed, rejected
        # Spread across clients so this measures the limiter rather than
        # contention on a single Redis key.
        identifier = f"client-{index % clients}"
        async with semaphore:
            started = time.perf_counter()
            result = await limiter.check(identifier, ROUTE)
            latencies.append((time.perf_counter() - started) * 1000)
            if result.allowed:
                allowed += 1
            else:
                rejected += 1

    started = time.perf_counter()
    await asyncio.gather(*(one_request(i) for i in range(requests)))
    duration = time.perf_counter() - started

    memory = await measure_memory(client, limiter, clients)

    return Result(
        algorithm=name,
        requests=requests,
        concurrency=concurrency,
        duration_seconds=duration,
        latencies_ms=latencies,
        allowed=allowed,
        rejected=rejected,
        bytes_per_client=memory,
    )


def print_report(results: list[Result], clients: int) -> None:
    print()
    header = (
        f"{'Algorithm':<16} {'Throughput':>12} {'Avg':>9} "
        f"{'P95':>9} {'P99':>9} {'Bytes/client':>13}"
    )
    print(header)
    print("-" * 72)
    for r in results:
        print(
            f"{r.algorithm:<16} {r.throughput:>9.0f}/s "
            f"{r.avg_latency_ms:>8.3f}ms {r.p95_latency_ms:>8.3f}ms "
            f"{r.p99_latency_ms:>8.3f}ms {r.bytes_per_client:>13.0f}"
        )

    print()
    print("Decisions:")
    for r in results:
        print(f"  {r.algorithm:<16} allowed {r.allowed:>6}   rejected {r.rejected:>6}")

    print()
    print(f"{clients} distinct clients. Memory is Redis MEMORY USAGE per client key.")


def print_markdown(results: list[Result]) -> None:
    """Emit the table in the shape the README uses."""
    print()
    print("| Algorithm | Throughput (req/sec) | Avg latency | P95 latency | Memory per client |")
    print("|---|---|---|---|---|")
    complexity = {
        "fixed_window": "O(1)",
        "token_bucket": "O(1)",
        "sliding_window": "O(requests)",
    }
    for r in results:
        print(
            f"| {r.algorithm.replace('_', ' ').title()} | {r.throughput:,.0f} | "
            f"{r.avg_latency_ms:.2f} ms | {r.p95_latency_ms:.2f} ms | "
            f"{r.bytes_per_client:.0f} bytes ({complexity[r.algorithm]}) |"
        )


async def main_async(args: argparse.Namespace) -> int:
    client = redis.from_url(
        args.redis_url,
        decode_responses=True,
        max_connections=args.concurrency + 10,
    )

    try:
        await client.ping()
    except Exception as exc:
        print(f"Cannot reach Redis at {args.redis_url}: {exc}", file=sys.stderr)
        print("Start one with: docker compose -f docker-compose.test.yml up -d", file=sys.stderr)
        return 1

    info = await client.info("server")
    print(f"Redis {info.get('redis_version')} at {args.redis_url}")
    print(f"{args.requests} requests, {args.concurrency} concurrent, {args.clients} clients")
    print(f"Limit {args.limit} per {args.window}s")

    results = []
    for name in ALGORITHMS:
        print(f"  running {name}...", flush=True)
        results.append(
            await run_one(
                client,
                name,
                requests=args.requests,
                concurrency=args.concurrency,
                clients=args.clients,
                limit=args.limit,
                window=args.window,
            )
        )

    async for key in client.scan_iter(match="ratelimit:*", count=1000):
        await client.delete(key)
    await client.aclose()

    print_report(results, args.clients)
    if args.markdown:
        print_markdown(results)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark the rate limiting algorithms")
    parser.add_argument("--requests", type=int, default=10000)
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument(
        "--clients",
        type=int,
        default=100,
        help="distinct client keys, so this measures the limiter and not contention on one key",
    )
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--window", type=int, default=60)
    parser.add_argument("--redis-url", default="redis://localhost:6399/0")
    parser.add_argument("--markdown", action="store_true", help="also print the README table")
    args = parser.parse_args()

    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
