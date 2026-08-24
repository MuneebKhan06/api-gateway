import pytest

from gateway.health import distinct_upstreams, overall_status
from gateway.schemas.gateway import RouteConfig
from upstream.service_a import main as service_a
from upstream.service_b import main as service_b


class TestUpstreamCollection:
    def test_routes_sharing_an_upstream_are_collapsed(self):
        routes = [
            RouteConfig(path_prefix="/api/orders", upstream="http://service-a:8001"),
            RouteConfig(path_prefix="/api/carts", upstream="http://service-a:8001"),
            RouteConfig(path_prefix="/api/users", upstream="http://service-b:8002"),
        ]
        assert distinct_upstreams(routes) == {
            "service-a": "http://service-a:8001",
            "service-b": "http://service-b:8002",
        }

    def test_gateway_owned_routes_are_skipped(self):
        routes = [RouteConfig(path_prefix="/health", upstream=None)]
        assert distinct_upstreams(routes) == {}


class TestOverallStatus:
    def test_all_healthy(self):
        assert overall_status({"service-a": "healthy"}) == "healthy"

    def test_one_bad_upstream_degrades(self):
        assert overall_status({"a": "healthy", "b": "unreachable"}) == "degraded"

    def test_no_upstreams_is_healthy(self):
        assert overall_status({}) == "healthy"


class TestHealthEndpoint:
    def test_reports_every_upstream(self, gateway):
        body = gateway.get("/health").json()
        assert body["upstreams"]["service-a"] == "healthy"
        assert body["upstreams"]["service-b"] == "healthy"
        assert body["upstreams"]["service-c"] == "healthy"

    def test_unreachable_upstream_shows_up(self, gateway):
        # /api/missing points at a host the mock transport does not know.
        body = gateway.get("/health").json()
        assert body["upstreams"]["nowhere"] == "unreachable"
        assert body["status"] == "degraded"

    def test_failing_upstream_is_reported_unhealthy(self, gateway):
        service_a._state["fail"] = True
        body = gateway.get("/health").json()
        assert body["upstreams"]["service-a"] == "unhealthy"
        assert body["status"] == "degraded"

    def test_health_stays_200_when_an_upstream_is_down(self, gateway):
        service_b._state["fail"] = True
        # Degraded is still a 200: the other routes are fine.
        assert gateway.get("/health").status_code == 200
