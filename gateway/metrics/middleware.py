"""Metrics middleware.

Outermost of the functional middleware, just inside correlation, so the
duration it measures is the whole time the gateway held the request: auth,
rate limiting, breaker check, proxying, everything. That is the number a
client actually experiences.

The upstream label comes from the matched route, not the request path. A path
label would be unbounded, since anyone can request any URL, and unbounded
labels are how a Prometheus server runs out of memory. Requests that match no
route are labelled once, as `unmatched`, rather than creating a series each.
"""

import logging
import time

from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from gateway.metrics.prometheus import (
    classify_error,
    observe_error,
    observe_request,
    requests_in_flight,
)
from gateway.router import RouteNotFound

logger = logging.getLogger(__name__)

UNMATCHED = "unmatched"


class MetricsMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)

        # Scraping the metrics endpoint should not itself be measured. It runs
        # every few seconds forever and would drown the real traffic.
        if request.url.path == "/metrics":
            await self.app(scope, receive, send)
            return

        upstream = self._upstream_label(request)
        method = request.method

        outcome: dict = {}

        async def send_with_timing(message: Message) -> None:
            if message["type"] == "http.response.start":
                outcome["status"] = message["status"]
            await send(message)

        requests_in_flight.labels(upstream=upstream).inc()
        started = time.perf_counter()

        try:
            await self.app(scope, receive, send_with_timing)
        except Exception:
            # An unhandled crash still gets counted, otherwise the metric that
            # matters most during an incident is the one that goes quiet.
            duration = time.perf_counter() - started
            observe_request(upstream, method, 500, duration)
            observe_error(upstream, method, "unhandled_exception")
            raise
        finally:
            requests_in_flight.labels(upstream=upstream).dec()

        duration = time.perf_counter() - started
        status = outcome.get("status", 500)

        observe_request(upstream, method, status, duration)

        error_type = classify_error(status)
        if error_type is not None:
            observe_error(upstream, method, error_type)

    @staticmethod
    def _upstream_label(request: Request) -> str:
        """Bounded label: the matched route's upstream, or a single bucket for
        anything unmatched."""
        table = getattr(request.app.state, "route_table", None)
        if table is None:
            return UNMATCHED
        try:
            return table.match(request.url.path).name
        except RouteNotFound:
            return UNMATCHED
