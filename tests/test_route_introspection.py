"""GET /gateway/routes reports what is actually being enforced.

The endpoint existed since Day 1 but reported a placeholder for the rate
limit. Now it reports the limiter the gateway would really use.
"""


def routes_by_prefix(gateway) -> dict:
    return {route["path_prefix"]: route for route in gateway.get("/gateway/routes").json()}


class TestRouteListing:
    def test_every_configured_route_is_listed(self, gateway):
        listed = routes_by_prefix(gateway)
        assert {"/api/orders", "/api/limited", "/api/protected", "/health"} <= set(listed)

    def test_proxy_settings_are_reported(self, gateway):
        route = routes_by_prefix(gateway)["/api/orders"]
        assert route["upstream"] == "http://service-a:8001"
        assert route["strip_prefix"] is True
        assert route["timeout_seconds"] == 5

    def test_auth_requirement_is_reported(self, gateway):
        listed = routes_by_prefix(gateway)
        assert listed["/api/protected"]["auth_required"] is True
        assert listed["/api/orders"]["auth_required"] is False

    def test_gateway_owned_routes_report_no_upstream(self, gateway):
        health = routes_by_prefix(gateway)["/health"]
        assert health["upstream"] is None
        assert health["circuit_breaker_state"] == "disabled"


class TestRateLimitReporting:
    def test_unlimited_route_reports_no_limit(self, gateway):
        assert routes_by_prefix(gateway)["/api/orders"]["rate_limit"] is None

    def test_limited_route_reports_its_limit(self, gateway):
        limit = routes_by_prefix(gateway)["/api/limited"]["rate_limit"]
        assert limit["algorithm"] == "fixed_window"
        assert limit["requests"] == 3
        assert limit["window_seconds"] == 60

    def test_algorithm_is_reported_per_route(self, gateway):
        listed = routes_by_prefix(gateway)
        assert listed["/api/limited"]["rate_limit"]["algorithm"] == "fixed_window"
        assert listed["/api/limited-bucket"]["rate_limit"]["algorithm"] == "token_bucket"

    def test_derived_per_second_rate_is_reported(self, gateway):
        limit = routes_by_prefix(gateway)["/api/limited"]["rate_limit"]
        assert limit["requests_per_second"] == 0.05

    def test_reported_limit_matches_what_is_enforced(self, gateway):
        """The number in the listing has to be the number that actually
        rejects the request, or the endpoint is worse than useless."""
        reported = routes_by_prefix(gateway)["/api/limited"]["rate_limit"]["requests"]

        allowed = 0
        for _ in range(reported + 3):
            if gateway.get("/api/limited").status_code == 200:
                allowed += 1

        assert allowed == reported
