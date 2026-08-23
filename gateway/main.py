"""Gateway entry point.

Right now this serves the gateway's own endpoints: health and route
introspection. The proxy and the middleware chain are layered on top of this
app as they are built.
"""

import contextlib
import logging
import signal
from collections.abc import AsyncIterator

from fastapi import FastAPI

from gateway.config import Settings, get_settings
from gateway.logging_config import configure_logging
from gateway.router import RouteTable, build_route_table
from gateway.schemas.gateway import RouteStatus

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

    logger.info("Gateway started in %s mode", settings.environment)
    yield
    logger.info("Gateway shutting down")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    app = FastAPI(
        title="API Gateway",
        version="0.1.0",
        description="Reverse proxy with JWT auth, rate limiting and circuit breaking",
        lifespan=lifespan,
    )
    app.state.settings = settings

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

    return app


app = create_app()
