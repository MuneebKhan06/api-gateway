"""Breaker introspection and manual control.

Two things an operator needs during an incident that the automatic behaviour
does not give them:

- See which breakers are open right now, without reading logs.
- Override. An upstream that has been fixed should not have to wait out a
  recovery timeout, and one that is known-bad should be removable from
  rotation before it starts failing.

These endpoints require authentication, unlike /health. Tripping a breaker
takes a service offline for every client of the gateway, which is not
something anonymous callers should be able to do.
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from gateway.circuit_breaker.breaker import BreakerState
from gateway.middleware.correlation import get_request_id
from gateway.schemas.gateway import BreakerStatus, GatewayError, MessageResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/gateway/breakers", tags=["gateway"])


def _known_upstreams(request: Request) -> dict[str, str]:
    """Upstream name to URL, for every route with a breaker enabled."""
    return {
        route.name: route.upstream
        for route in request.app.state.route_table.all()
        if route.circuit_breaker and route.upstream is not None
    }


def _not_found(upstream: str) -> JSONResponse:
    body = GatewayError(
        error="unknown_upstream",
        detail=f"no route in the table forwards to {upstream!r}",
        request_id=get_request_id(),
    )
    return JSONResponse(status_code=404, content=body.model_dump())


@router.get("", response_model=list[BreakerStatus])
async def list_breakers(request: Request) -> list[BreakerStatus]:
    registry = request.app.state.circuit_breakers
    upstreams = _known_upstreams(request)

    statuses = []
    for name in sorted(upstreams):
        snapshot = await registry.snapshot(name)
        statuses.append(
            BreakerStatus(
                upstream=name,
                url=upstreams[name],
                state=snapshot.state.value,
                failures=snapshot.failures,
                successes=snapshot.successes,
                opened_at=snapshot.opened_at,
                recovery_timeout_seconds=registry.config.recovery_timeout_seconds,
                failure_threshold=registry.config.failure_threshold,
            )
        )
    return statuses


@router.post("/{upstream}/reset", response_model=MessageResponse)
async def reset_breaker(upstream: str, request: Request):
    """Force a breaker closed, for an upstream known to be healthy again."""
    if upstream not in _known_upstreams(request):
        return _not_found(upstream)

    await request.app.state.circuit_breakers.reset(upstream)
    logger.info("Breaker for %s reset by an operator", upstream)
    return MessageResponse(message=f"circuit breaker for {upstream} is now closed")


@router.post("/{upstream}/trip", response_model=MessageResponse)
async def trip_breaker(upstream: str, request: Request):
    """Force a breaker open, to take an upstream out of rotation deliberately.

    It still recovers on its own once the timeout lapses, so this is a way to
    shed traffic now rather than a permanent switch.
    """
    if upstream not in _known_upstreams(request):
        return _not_found(upstream)

    await request.app.state.circuit_breakers.trip(upstream)
    logger.warning("Breaker for %s tripped open by an operator", upstream)
    return MessageResponse(
        message=f"circuit breaker for {upstream} is now open",
    )


@router.get("/{upstream}", response_model=BreakerStatus)
async def get_breaker(upstream: str, request: Request):
    upstreams = _known_upstreams(request)
    if upstream not in upstreams:
        return _not_found(upstream)

    registry = request.app.state.circuit_breakers
    snapshot = await registry.snapshot(upstream)
    return BreakerStatus(
        upstream=upstream,
        url=upstreams[upstream],
        state=snapshot.state.value,
        failures=snapshot.failures,
        successes=snapshot.successes,
        opened_at=snapshot.opened_at,
        recovery_timeout_seconds=registry.config.recovery_timeout_seconds,
        failure_threshold=registry.config.failure_threshold,
    )


__all__ = ["router", "BreakerState"]
