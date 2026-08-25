from fastapi.testclient import TestClient

from gateway.config import Settings
from gateway.main import create_app


def make_client() -> TestClient:
    settings = Settings(routes_file="routes.yaml", environment="test")
    return TestClient(create_app(settings))


def test_health_reports_loaded_routes():
    """This app is built with no injected fakes, so nothing it depends on is
    reachable. It should say so rather than claim to be healthy.

    tests/test_health.py covers the healthy and degraded paths against a fully
    wired gateway.
    """
    with make_client() as client:
        response = client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["routes_loaded"] > 0
        assert body["status"] == "unhealthy"
        assert body["redis"] == "unavailable"
        assert body["database"] == "unavailable"
        assert set(body["upstreams"]) == {"service-a", "service-b", "service-c"}


def test_routes_endpoint_lists_the_table():
    with make_client() as client:
        response = client.get("/gateway/routes")
        assert response.status_code == 200

        routes = response.json()
        prefixes = {route["path_prefix"] for route in routes}
        assert {"/api/orders", "/api/users", "/api/inventory"} <= prefixes


def test_gateway_owned_routes_report_breaker_disabled():
    with make_client() as client:
        routes = client.get("/gateway/routes").json()
        health = next(r for r in routes if r["path_prefix"] == "/health")
        assert health["upstream"] is None
        assert health["circuit_breaker_state"] == "disabled"
