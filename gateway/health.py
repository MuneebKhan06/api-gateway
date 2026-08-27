"""Health aggregation.

The gateway's own liveness is trivial: if the process answers, it is alive.
The useful signal is whether the things it depends on are reachable, so
/health probes every distinct upstream in the route table.

Probes run concurrently. Doing them in sequence would make the health check
as slow as the sum of all upstream latencies, and health checks are polled
often enough that this matters.
"""

import asyncio
import logging

import httpx

from gateway.proxy import ReverseProxy
from gateway.schemas.gateway import RouteConfig

logger = logging.getLogger(__name__)

# Deliberately short. A health probe that hangs for 30 seconds is worse than
# one that reports a slow upstream as unhealthy.
PROBE_TIMEOUT_SECONDS = 2.0


def distinct_upstreams(routes: list[RouteConfig]) -> dict[str, str]:
    """Map service name to base URL, one entry per upstream, not per route."""
    upstreams: dict[str, str] = {}
    for route in routes:
        if route.upstream is not None:
            upstreams.setdefault(route.name, route.upstream.rstrip("/"))
    return upstreams


async def probe_upstream(proxy: ReverseProxy, name: str, base_url: str) -> tuple[str, str]:
    """Return (name, status) where status is healthy, unhealthy or unreachable."""
    try:
        response = await proxy.client.get(f"{base_url}/health", timeout=PROBE_TIMEOUT_SECONDS)
    except httpx.TimeoutException:
        return name, "unreachable"
    except httpx.RequestError:
        return name, "unreachable"

    if response.status_code == 200:
        return name, "healthy"
    return name, "unhealthy"


async def check_upstreams(
    proxy: ReverseProxy, routes: list[RouteConfig], breaker_states: dict | None = None
) -> dict[str, str]:
    """Probe every upstream, skipping those the breaker has already given up on.

    An open breaker means the gateway has already decided this upstream is
    unwell and stopped sending it traffic. Probing it anyway adds load to a
    service that is struggling, and the answer is one the gateway already
    knows. So the breaker state is reported directly instead.

    This also keeps /health cheap during an outage, which is when it tends to
    be polled hardest.
    """
    upstreams = distinct_upstreams(routes)
    if not upstreams:
        return {}

    breaker_states = breaker_states or {}
    results: dict[str, str] = {}
    to_probe: dict[str, str] = {}

    for name, url in upstreams.items():
        state = breaker_states.get(name)
        if state is not None and getattr(state, "value", state) == "open":
            results[name] = "circuit_open"
        else:
            to_probe[name] = url

    if to_probe:
        probed = await asyncio.gather(
            *(probe_upstream(proxy, name, url) for name, url in to_probe.items())
        )
        results.update(dict(probed))

    return dict(sorted(results.items()))


async def check_dependencies(redis_client, database) -> dict[str, str]:
    """Probe Redis and Postgres side by side.

    Both are probed even when the first one is already down, because a health
    endpoint that stops at the first failure hides the second one.
    """
    redis_ok, database_ok = await asyncio.gather(
        redis_client.ping(),
        database.ping(),
    )
    return {
        "redis": "connected" if redis_ok else "unavailable",
        "database": "connected" if database_ok else "unavailable",
    }


def overall_status(
    upstream_states: dict[str, str], dependencies: dict[str, str] | None = None
) -> str:
    """Report healthy, degraded or unhealthy.

    The distinction matters to whatever is polling this:

    - degraded: an upstream is down, but the gateway still serves every other
      route correctly. Stays a 200, because pulling the gateway out of
      rotation would take out the routes that are still working.
    - unhealthy: Redis or Postgres is gone. Authentication and rate limiting
      cannot be enforced, so the gateway itself is the problem.
    """
    dependencies = dependencies or {}

    if any(state != "connected" for state in dependencies.values()):
        return "unhealthy"
    if any(state != "healthy" for state in upstream_states.values()):
        return "degraded"
    return "healthy"


def probe_count(upstream_states: dict[str, str]) -> int:
    """How many upstreams were actually contacted, as opposed to reported
    from breaker state. Useful when reasoning about health check cost."""
    return sum(1 for state in upstream_states.values() if state != "circuit_open")
