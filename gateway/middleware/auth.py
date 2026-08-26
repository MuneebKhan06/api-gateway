"""JWT authentication middleware.

Sits between correlation and the proxy. For every request it finds the route,
and if that route says auth_required it insists on a valid, unexpired,
unblacklisted access token before anything is forwarded.

Validation order is deliberate and cheapest-first:

1. Does the route even need auth? If not, stop here.
2. Is there a bearer token? No network call needed to answer that.
3. Does the signature verify and is it unexpired? Local CPU only.
4. Is the jti blacklisted? This is the only step that touches Redis, so it
   runs last and only for tokens that are otherwise valid.

Doing the Redis lookup before signature validation would let anyone drive
gateway Redis traffic with garbage tokens.
"""

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from gateway.auth.blacklist import TokenBlacklist
from gateway.auth.jwt_handler import (
    TokenClaims,
    TokenExpired,
    TokenInvalid,
    WrongTokenType,
    extract_bearer_token,
)
from gateway.middleware.correlation import get_request_id
from gateway.router import RouteNotFound
from gateway.schemas.gateway import GatewayError

logger = logging.getLogger(__name__)


def _unauthorized(code: str, detail: str) -> JSONResponse:
    body = GatewayError(error=code, detail=detail, request_id=get_request_id())
    return JSONResponse(
        status_code=401,
        content=body.model_dump(),
        # RFC 9110 says a 401 must carry this, and it tells a client which
        # scheme to retry with.
        headers={"WWW-Authenticate": "Bearer"},
    )


class AuthMiddleware:
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
            # Not this middleware's problem. The proxy layer owns the 404, and
            # answering here would mean an unknown path returns 401 instead.
            await self.app(scope, receive, send)
            return

        if not route.auth_required:
            await self.app(scope, receive, send)
            return

        token = extract_bearer_token(request.headers.get("authorization"))
        if token is None:
            response = _unauthorized("missing_token", "an access token is required")
            await response(scope, receive, send)
            return

        try:
            claims: TokenClaims = state.jwt_handler.decode_access(token)
        except TokenExpired:
            response = _unauthorized("token_expired", "this access token has expired")
            await response(scope, receive, send)
            return
        except WrongTokenType:
            response = _unauthorized(
                "wrong_token_type", "a refresh token cannot be used to authenticate a request"
            )
            await response(scope, receive, send)
            return
        except TokenInvalid:
            response = _unauthorized("invalid_token", "this access token is not valid")
            await response(scope, receive, send)
            return

        blacklist = TokenBlacklist(state.redis.client, cache=state.blacklist_cache)
        if await blacklist.contains(claims.jti):
            logger.info("Rejected blacklisted token %s", claims.jti)
            response = _unauthorized("token_revoked", "this access token has been revoked")
            await response(scope, receive, send)
            return

        # Hand the identity to everything downstream. The proxy reads this to
        # tell the upstream who is calling.
        scope["user"] = {
            "id": claims.subject,
            "email": claims.email,
            "roles": claims.roles,
        }

        await self.app(scope, receive, send)
