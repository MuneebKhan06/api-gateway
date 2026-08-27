"""Circuit breaker middleware.

Innermost of the four, so it sits closest to the proxy. A request that gets
here has already been identified, authenticated and rate limited; the only
question left is whether the upstream is in a fit state to receive it.

Two jobs:

1. Before the call, ask the breaker for permission. An open breaker means the
   request is refused here, immediately, without opening a connection.
2. After the call, report what happened, so the breaker learns.

The refusal is a 503 rather than a 502. 502 says "I tried and the upstream
failed"; 503 says "I am not trying right now". The second is honest, and it is
also what a client should back off on.
"""

import logging
import math

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from gateway.circuit_breaker.breaker import BreakerState, counts_as_failure
from gateway.middleware.correlation import get_request_id
from gateway.router import RouteNotFound
from gateway.schemas.gateway import GatewayError

logger = logging.getLogger(__name__)

BREAKER_STATE_HEADER = "x-circuit-breaker"


class CircuitBreakerMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        state = request.app.state

        try:
            route = state.route_table.match(request.url.path)
        except RouteNotFound:
            await self.app(scope, receive, send)
            return

        if not route.circuit_breaker or route.upstream is None:
            await self.app(scope, receive, send)
            return

        registry = state.circuit_breakers
        upstream = route.name

        allowed, breaker_state = await registry.allow_request(upstream)
        if not allowed:
            logger.info("Refused request to %s, circuit breaker is open", upstream)
            response = self._service_unavailable(upstream, registry)
            await response(scope, receive, send)
            return

        # Watch the response status on the way out to decide success or
        # failure. The status is the only signal available here, since the
        # proxy has already turned transport errors into 502 and 504.
        status_seen: dict[str, int] = {}

        async def send_with_outcome(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_seen["status"] = message["status"]
                headers = list(message.get("headers") or [])
                headers.append(
                    (BREAKER_STATE_HEADER.encode(), breaker_state.value.encode())
                )
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_outcome)
        except Exception:
            # Never reached in normal operation, since the proxy maps its own
            # errors to status codes. Here so an unexpected crash still counts
            # against the upstream instead of being invisible to the breaker.
            await registry.record_failure(upstream)
            raise

        status = status_seen.get("status")
        if counts_as_failure(status):
            await registry.record_failure(upstream)
        else:
            await registry.record_success(upstream)

    @staticmethod
    def _service_unavailable(upstream: str, registry) -> JSONResponse:
        body = GatewayError(
            error="circuit_open",
            detail=f"{upstream} is not accepting requests right now",
            request_id=get_request_id(),
        )
        response = JSONResponse(status_code=503, content=body.model_dump())
        response.headers[BREAKER_STATE_HEADER] = BreakerState.OPEN.value
        # Tell the client roughly when it is worth trying again, rather than
        # letting it hammer a service that is already struggling.
        response.headers["retry-after"] = str(
            int(math.ceil(registry.config.recovery_timeout_seconds))
        )
        return response
