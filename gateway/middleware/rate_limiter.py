"""Rate limiting middleware.

Runs after auth, which matters: an authenticated request is limited per user,
and only an anonymous one falls back to the client IP. Limiting by IP when a
user identity is available would put everyone behind one office NAT into a
shared bucket.

Responses carry the standard X-RateLimit headers whether or not the request
was allowed, so a well behaved client can slow itself down before being
rejected rather than discovering the limit by hitting it.
"""

import logging
import math

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from gateway.metrics.prometheus import observe_rate_limit
from gateway.middleware.correlation import get_request_id
from gateway.rate_limit.base import RateLimitResult
from gateway.router import RouteNotFound
from gateway.schemas.gateway import GatewayError

logger = logging.getLogger(__name__)

LIMIT_HEADER = b"x-ratelimit-limit"
REMAINING_HEADER = b"x-ratelimit-remaining"
RESET_HEADER = b"x-ratelimit-reset"
RETRY_AFTER_HEADER = b"retry-after"


def client_identifier(request: Request) -> str:
    """Who to charge this request to.

    An authenticated user is identified by user id, which follows them across
    addresses. Anonymous traffic falls back to the client address.
    """
    user = request.scope.get("user")
    if user and user.get("id"):
        return f"user:{user['id']}"

    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        # Left-most entry is the original client. Trusted because the gateway
        # is expected to sit behind a load balancer that sets it.
        return f"ip:{forwarded.split(',')[0].strip()}"

    host = request.client.host if request.client else "unknown"
    return f"ip:{host}"


def rate_limit_headers(result: RateLimitResult) -> list[tuple[bytes, bytes]]:
    headers = [
        (LIMIT_HEADER, str(result.limit).encode()),
        (REMAINING_HEADER, str(result.remaining).encode()),
        # Whole seconds, rounded up: reporting 0 for a window that has not
        # actually reset invites a client to retry immediately and fail again.
        (RESET_HEADER, str(int(math.ceil(result.reset_after))).encode()),
    ]
    if result.retry_after is not None:
        headers.append((RETRY_AFTER_HEADER, str(int(math.ceil(result.retry_after))).encode()))
    return headers


class RateLimitMiddleware:
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
            # The proxy layer owns the 404. Answering here would mean an
            # unknown path could be rate limited into a 429 instead.
            await self.app(scope, receive, send)
            return

        if route.rate_limit is None:
            await self.app(scope, receive, send)
            return

        limiter = state.rate_limiters.get(route.rate_limit)
        identifier = client_identifier(request)
        result = await limiter.check(identifier, route.path_prefix)

        # Labelled by route prefix, not by client: per client series would be
        # unbounded, and the useful question is which routes are throttling.
        observe_rate_limit(limiter.name, route.path_prefix, result.allowed)

        if result.rejected:
            logger.info(
                "Rate limited %s on %s (%s, %d per %ds)",
                identifier,
                route.path_prefix,
                limiter.name,
                limiter.limit,
                limiter.window_seconds,
            )
            response = self._too_many_requests(result)
            await response(scope, receive, send)
            return

        # Allowed, so let it through and attach the headers on the way out.
        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                headers.extend(rate_limit_headers(result))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)

    @staticmethod
    def _too_many_requests(result: RateLimitResult) -> JSONResponse:
        body = GatewayError(
            error="rate_limit_exceeded",
            detail="too many requests, slow down",
            request_id=get_request_id(),
        )
        response = JSONResponse(status_code=429, content=body.model_dump())
        for key, value in rate_limit_headers(result):
            response.headers[key.decode()] = value.decode()
        return response
