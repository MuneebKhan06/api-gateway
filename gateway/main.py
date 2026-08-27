"""Gateway entry point.

Builds the app, owns the lifespan, and serves the gateway's own endpoints
(health and route introspection). Everything else falls through to the
catch-all proxy route at the bottom of this module.

Lifespan starts three shared, process-wide resources: the proxy's HTTP client,
the Redis pool and the database engine. All three are injectable so tests can
run the real request path against fakes.
"""

import asyncio
import contextlib
import logging
import signal
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from gateway.auth.blacklist_cache import BlacklistCache
from gateway.auth.routes import build_jwt_handler
from gateway.auth.routes import router as auth_router
from gateway.circuit_breaker.registry import build_registry
from gateway.config import Settings, get_settings
from gateway.db.connection import Database
from gateway.health import check_dependencies, check_upstreams, overall_status
from gateway.logging_config import configure_logging
from gateway.middleware.auth import AuthMiddleware
from gateway.middleware.correlation import CorrelationIdMiddleware, get_request_id
from gateway.middleware.rate_limiter import RateLimitMiddleware
from gateway.proxy import ReverseProxy, UpstreamTimeout, UpstreamUnavailable
from gateway.rate_limit.factory import RateLimiterRegistry
from gateway.redis_client import RedisClient
from gateway.router import RouteNotFound, RouteTable, build_route_table
from gateway.schemas.gateway import GatewayError, RateLimitStatus, RouteStatus

logger = logging.getLogger(__name__)


def _install_sighup_handler(app: FastAPI, table: RouteTable) -> None:
    """Reload the route table on SIGHUP, the way Nginx does.

    Not every platform has SIGHUP (Windows does not), so a missing signal is
    logged and ignored rather than crashing startup.
    """

    def handle_sighup(signum, frame) -> None:  # noqa: ARG001
        logger.info("SIGHUP received, reloading route table")
        if table.reload():
            # Limiters are cached per configuration, so a changed limit would
            # otherwise keep being served from the old instance.
            registry = getattr(app.state, "rate_limiters", None)
            if registry is not None:
                registry.clear()
            breakers = getattr(app.state, "circuit_breakers", None)
            if breakers is not None:
                breakers.clear()

    try:
        signal.signal(signal.SIGHUP, handle_sighup)
    except (AttributeError, ValueError):
        logger.warning("SIGHUP reload is not available on this platform")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    configure_logging(settings.log_level)

    table = build_route_table(settings.routes_file)
    app.state.route_table = table
    _install_sighup_handler(app, table)

    proxy = getattr(app.state, "proxy", None) or ReverseProxy(
        max_connections=settings.upstream_max_connections,
        default_timeout=settings.upstream_timeout_seconds,
    )
    await proxy.startup()
    app.state.proxy = proxy

    redis_client = getattr(app.state, "redis", None) or RedisClient(
        settings.redis_url, max_connections=settings.redis_max_connections
    )
    await redis_client.startup()
    app.state.redis = redis_client

    database = getattr(app.state, "database", None) or Database(settings.database_url)
    await database.startup()
    app.state.database = database

    app.state.jwt_handler = build_jwt_handler(settings)
    app.state.rate_limiters = RateLimiterRegistry(redis_client.client)
    app.state.circuit_breakers = build_registry(redis_client.client, settings)
    # One cache for the process, shared by the auth middleware and the logout
    # handler so a revocation is visible to both immediately.
    app.state.blacklist_cache = BlacklistCache(
        ttl_seconds=settings.blacklist_cache_ttl_seconds
    )

    logger.info("Gateway started in %s mode", settings.environment)
    try:
        yield
    finally:
        # Shut down in reverse order of startup.
        await database.shutdown()
        await redis_client.shutdown()
        await proxy.shutdown()
        logger.info("Gateway shutting down")


def create_app(
    settings: Settings | None = None,
    proxy: ReverseProxy | None = None,
    redis_client: RedisClient | None = None,
    database: Database | None = None,
) -> FastAPI:
    """Build the app.

    The three collaborators are injectable so tests can supply fakes: a proxy
    pointed at in-process upstreams, a fake Redis, an in-memory database. Left
    as None in normal operation, where lifespan builds them from settings.
    """
    settings = settings or get_settings()

    app = FastAPI(
        title="API Gateway",
        version="0.1.0",
        description="Reverse proxy with JWT auth, rate limiting and circuit breaking",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.proxy = proxy
    app.state.redis = redis_client
    app.state.database = database

    # Order matters, and add_middleware stacks in reverse: the last one
    # added runs first, so the chain is correlation, auth, rate limit.
    #
    # Correlation is outermost so even a 401 or 429 carries a request ID.
    # Rate limiting runs after auth so an authenticated request is charged to
    # its user rather than to whatever address it came from.
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(AuthMiddleware)
    app.add_middleware(CorrelationIdMiddleware)

    @app.get("/health", tags=["gateway"])
    async def health() -> dict:
        """Health check.

        Reports the gateway plus every upstream it knows about. Redis and
        Postgres are added here once they are wired in.
        """
        routes = app.state.route_table.all()
        upstreams, dependencies = await asyncio.gather(
            check_upstreams(app.state.proxy, routes),
            check_dependencies(app.state.redis, app.state.database),
        )
        return {
            "status": overall_status(upstreams, dependencies),
            "routes_loaded": len(routes),
            **dependencies,
            "upstreams": upstreams,
        }

    @app.get("/gateway/routes", tags=["gateway"], response_model=list[RouteStatus])
    async def list_routes() -> list[RouteStatus]:
        """Show the route table as the gateway is currently enforcing it."""
        return [_route_status(app, route) for route in app.state.route_table.all()]

    # Registered before the catch-all so /auth is served here, not proxied.
    app.include_router(auth_router)

    _register_error_handlers(app)
    _register_proxy_route(app)
    return app


def _rate_limit_status(app: FastAPI, route) -> RateLimitStatus | None:
    """Report the limit actually in force on a route.

    The limiter is looked up rather than read straight off the config, so a
    route whose limiter could not be built reports no limit instead of
    claiming one that is not being applied.
    """
    if route.rate_limit is None:
        return None

    registry = getattr(app.state, "rate_limiters", None)
    if registry is None:
        return None

    try:
        limiter = registry.get(route.rate_limit)
    except ValueError:
        logger.warning(
            "Route %s declares an unusable rate limit, reporting it as unlimited",
            route.path_prefix,
        )
        return None

    return RateLimitStatus(
        algorithm=limiter.name,
        requests=limiter.limit,
        window_seconds=limiter.window_seconds,
        requests_per_second=round(limiter.limit / limiter.window_seconds, 4),
    )


def _route_status(app: FastAPI, route) -> RouteStatus:
    return RouteStatus(
        path_prefix=route.path_prefix,
        upstream=route.upstream,
        strip_prefix=route.strip_prefix,
        timeout_seconds=route.timeout_seconds,
        auth_required=route.auth_required,
        # Filled in for real once the breaker registry exists on Day 5.
        circuit_breaker_state="closed" if route.circuit_breaker else "disabled",
        rate_limit=_rate_limit_status(app, route),
    )


def _error_response(status_code: int, error: str, detail: str) -> JSONResponse:
    body = GatewayError(error=error, detail=detail, request_id=get_request_id())
    return JSONResponse(status_code=status_code, content=body.model_dump())


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RouteNotFound)
    async def handle_route_not_found(request: Request, exc: RouteNotFound) -> JSONResponse:
        return _error_response(404, "route_not_found", f"no route matches {request.url.path}")

    @app.exception_handler(UpstreamTimeout)
    async def handle_upstream_timeout(request: Request, exc: UpstreamTimeout) -> JSONResponse:
        # 504 rather than 500: the gateway is fine, the upstream ran long.
        return _error_response(504, "upstream_timeout", str(exc))

    @app.exception_handler(UpstreamUnavailable)
    async def handle_upstream_unavailable(
        request: Request, exc: UpstreamUnavailable
    ) -> JSONResponse:
        return _error_response(502, "upstream_unavailable", str(exc))


def _register_proxy_route(app: FastAPI) -> None:
    """Catch-all proxy handler.

    Registered last on purpose. Starlette matches routes in registration
    order, so /health and /gateway/routes above win before anything reaches
    this. Everything else is looked up in the route table and forwarded.
    """

    @app.api_route(
        "/{full_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
        include_in_schema=False,
    )
    async def proxy_request(request: Request, full_path: str):
        route = app.state.route_table.match(request.url.path)

        if route.upstream is None:
            # A gateway-owned prefix that nothing has implemented yet.
            return _error_response(
                404, "not_implemented", f"{request.url.path} is not served by this gateway yet"
            )

        return await app.state.proxy.forward(request, route)


app = create_app()
