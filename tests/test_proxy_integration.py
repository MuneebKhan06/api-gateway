"""End to end tests: a request enters the gateway and reaches a real upstream app."""

from upstream.service_a import main as service_a


class TestForwarding:
    def test_get_reaches_the_upstream(self, gateway):
        response = gateway.get("/api/orders")
        assert response.status_code == 200
        assert response.json()["service"] == "service-a"

    def test_path_remainder_is_preserved(self, gateway):
        response = gateway.get("/api/orders/a1f0")
        assert response.status_code == 200
        assert response.json()["customer"] == "ayesha"

    def test_each_prefix_reaches_its_own_service(self, gateway):
        assert gateway.get("/api/users").json()["service"] == "service-b"
        assert gateway.get("/api/inventory").json()["service"] == "service-c"

    def test_upstream_headers_come_back(self, gateway):
        response = gateway.get("/api/orders")
        assert response.headers["x-upstream-service"] == "service-a"

    def test_request_id_survives_the_proxy_hop(self, gateway):
        response = gateway.get("/api/orders", headers={"X-Request-ID": "trace123"})
        assert response.headers["x-request-id"] == "trace123"

    def test_query_string_is_forwarded(self, gateway):
        # service-a ignores unknown params, so a 200 proves the URL parsed.
        assert gateway.get("/api/orders?page=2&status=open").status_code == 200

    def test_post_body_is_forwarded(self, gateway):
        response = gateway.post("/api/orders", json={"customer": "bilal", "total": 12.5})
        assert response.status_code == 201
        assert response.json()["customer"] == "bilal"

    def test_patch_body_is_forwarded(self, gateway):
        response = gateway.patch("/api/inventory/SKU-1001", json={"delta": -2})
        assert response.status_code == 200
        assert response.json()["quantity"] == 40

    def test_strip_prefix_off_keeps_the_full_path(self, gateway):
        # /api/raw is not a path service-a serves, so it must 404 at the upstream.
        assert gateway.get("/api/raw").status_code == 404


class TestStatusPassthrough:
    def test_upstream_404_is_not_rewritten(self, gateway):
        response = gateway.get("/api/orders/does-not-exist")
        assert response.status_code == 404
        assert response.json()["detail"] == "order not found"

    def test_upstream_409_is_passed_through(self, gateway):
        response = gateway.patch("/api/inventory/SKU-1003", json={"delta": -1})
        assert response.status_code == 409

    def test_upstream_503_is_passed_through_untouched(self, gateway):
        service_a._state["fail"] = True
        response = gateway.get("/api/orders")
        assert response.status_code == 503
        assert response.json()["detail"] == "upstream is unwell"


class TestGatewayErrors:
    def test_unmatched_path_returns_404_from_the_gateway(self, gateway):
        response = gateway.get("/nothing/here")
        assert response.status_code == 404
        assert response.json()["error"] == "route_not_found"

    def test_unreachable_upstream_returns_502(self, gateway):
        response = gateway.get("/api/missing")
        assert response.status_code == 502
        assert response.json()["error"] == "upstream_unavailable"

    def test_slow_upstream_returns_504(self, gateway):
        service_a._state["latency_ms"] = 800  # route timeout is 0.25s
        response = gateway.get("/api/slow")
        assert response.status_code == 504
        assert response.json()["error"] == "upstream_timeout"

    def test_gateway_errors_carry_the_request_id(self, gateway):
        response = gateway.get("/nothing/here", headers={"X-Request-ID": "abc123"})
        assert response.json()["request_id"] == "abc123"

    def test_unimplemented_gateway_route_returns_404(self, gateway):
        # /auth has no upstream and is not built yet.
        response = gateway.post("/auth/login", json={})
        assert response.status_code == 404
        assert response.json()["error"] == "not_implemented"


class TestGatewayOwnedRoutesStillWin:
    def test_health_is_not_swallowed_by_the_catch_all(self, gateway):
        # The gateway's own health body, not an upstream's.
        assert "routes_loaded" in gateway.get("/health").json()

    def test_route_listing_is_not_swallowed_by_the_catch_all(self, gateway):
        assert gateway.get("/gateway/routes").status_code == 200
