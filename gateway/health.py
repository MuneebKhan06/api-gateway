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


async def check_upstreams(proxy: ReverseProxy, routes: list[RouteConfig]) -> dict[str, str]:
    upstreams = distinct_upstreams(routes)
    if not upstreams:
        return {}

    results = await asyncio.gather(
        *(probe_upstream(proxy, name, url) for name, url in upstreams.items())
    )
    return dict(sorted(results))


def overall_status(upstream_states: dict[str, str]) -> str:
    """The gateway is healthy on its own, degraded when an upstream is not.

    This stays a 200 either way. A load balancer pulling the gateway out of
    rotation because one upstream is down would take out the routes that are
    still working, which is the opposite of what we want.
    """
    if any(state != "healthy" for state in upstream_states.values()):
        return "degraded"
    return "healthy"
