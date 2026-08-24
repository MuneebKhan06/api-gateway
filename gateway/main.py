"""Gateway entry point.

Right now this serves the gateway's own endpoints: health and route
introspection. The proxy and the middleware chain are layered on top of this
app as they are built.
"""

import contextlib
import logging
import signal
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from gateway.config import Settings, get_settings
from gateway.logging_config import configure_logging
from gateway.middleware.correlation import CorrelationIdMiddleware, get_request_id
from gateway.proxy import ReverseProxy, UpstreamTimeout, UpstreamUnavailable
from gateway.router import RouteNotFound, RouteTable, build_route_table
from gateway.schemas.gateway import GatewayError, RouteStatus

logger = logging.getLogger(__name__)


def _install_sighup_handler(app: FastAPI, table: RouteTable) -> None:
    """Reload the route table on SIGHUP, the way Nginx does.

    Not every platform has SIGHUP (Windows does not), so a missing signal is
    logged and ignored rather than crashing startup.
    """

    def handle_sighup(signum, frame) -> None:  # noqa: ARG001
        logger.info("SIGHUP received, reloading route table")
        table.reload()

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

    logger.info("Gateway started in %s mode", settings.environment)
    try:
        yield
    finally:
        await proxy.shutdown()
        logger.info("Gateway shutting down")


def create_app(settings: Settings | None = None, proxy: ReverseProxy | None = None) -> FastAPI:
    """Build the app.

    `proxy` is injectable so tests can supply one pointed at in-process mock
    upstreams. In normal operation it is left as None and lifespan builds one
    from settings.
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

    # Outermost middleware, so every log line and every response carries the
    # request ID even if something further in the chain rejects the request.
    app.add_middleware(CorrelationIdMiddleware)

    @app.get("/health", tags=["gateway"])
    async def health() -> dict:
        """Liveness check.

        Dependency checks (Redis, Postgres, upstreams) are added as those
        components are wired in.
        """
        return {
            "status": "healthy",
            "routes_loaded": len(app.state.route_table.all()),
        }

    @app.get("/gateway/routes", tags=["gateway"], response_model=list[RouteStatus])
    async def list_routes() -> list[RouteStatus]:
        """Show the route table as the gateway currently sees it."""
        return [
            RouteStatus(
                path_prefix=route.path_prefix,
                upstream=route.upstream,
                auth_required=route.auth_required,
                # Filled in for real once the breaker registry exists.
                circuit_breaker_state="closed" if route.circuit_breaker else "disabled",
                rate_limit_algorithm=route.rate_limit.algorithm if route.rate_limit else None,
            )
            for route in app.state.route_table.all()
        ]

    _register_error_handlers(app)
    _register_proxy_route(app)
    return app


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
