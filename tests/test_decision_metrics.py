"""Rate limiter and authentication decision metrics."""

import pytest

from gateway.metrics.prometheus import REGISTRY

CREDENTIALS = {"email": "muneeb@example.com", "password": "password123"}


def sample(name: str, **labels) -> float:
    value = REGISTRY.get_sample_value(name, labels)
    return 0.0 if value is None else value


def decisions(algorithm: str, route: str, decision: str) -> float:
    return sample(
        "gateway_rate_limit_decisions_total",
        algorithm=algorithm,
        route=route,
        decision=decision,
    )


def auth(result: str, reason: str = "none") -> float:
    return sample("gateway_auth_attempts_total", result=result, reason=reason)


@pytest.fixture
def auth_header(gateway):
    gateway.post("/auth/register", json=CREDENTIALS)
    tokens = gateway.post("/auth/login", json=CREDENTIALS).json()
    return {"Authorization": f"Bearer {tokens['access_token']}"}


class TestRateLimitDecisions:
    def test_allowed_requests_are_counted(self, gateway):
        before = decisions("fixed_window", "/api/limited", "allowed")
        gateway.get("/api/limited")
        assert decisions("fixed_window", "/api/limited", "allowed") == before + 1

    def test_rejected_requests_are_counted(self, gateway):
        before = decisions("fixed_window", "/api/limited", "rejected")
        for _ in range(5):
            gateway.get("/api/limited")
        assert decisions("fixed_window", "/api/limited", "rejected") > before

    def test_algorithms_are_counted_separately(self, gateway):
        """Splitting by algorithm means the three can be compared on real
        traffic, not only in the benchmark."""
        before = decisions("token_bucket", "/api/limited-bucket", "allowed")
        gateway.get("/api/limited-bucket")
        assert decisions("token_bucket", "/api/limited-bucket", "allowed") == before + 1
        # The fixed window route is unaffected.
        assert decisions("token_bucket", "/api/limited", "allowed") == 0

    def test_unlimited_routes_record_nothing(self, gateway):
        before = decisions("fixed_window", "/api/orders", "allowed")
        gateway.get("/api/orders")
        assert decisions("fixed_window", "/api/orders", "allowed") == before

    def test_route_is_the_label_not_the_client(self, gateway):
        """Per client labels would be unbounded. The useful question is which
        routes are throttling, not which user hit a limit."""
        gateway.get("/api/limited")
        labels = {
            frozenset(s.labels)
            for metric in REGISTRY.collect()
            for s in metric.samples
            if metric.name == "gateway_rate_limit_decisions"
        }
        assert labels == {frozenset({"algorithm", "route", "decision"})}


class TestAuthDecisions:
    def test_accepted_tokens_are_counted(self, gateway, auth_header):
        before = auth("accepted")
        gateway.get("/api/protected", headers=auth_header)
        assert auth("accepted") == before + 1

    def test_missing_token_is_counted(self, gateway):
        before = auth("rejected", "missing_token")
        gateway.get("/api/protected")
        assert auth("rejected", "missing_token") == before + 1

    def test_forged_token_is_counted_distinctly(self, gateway):
        """A tampered signature is an attack signal and should not be lost
        among ordinary expiries."""
        before = auth("rejected", "invalid_token")
        gateway.get("/api/protected", headers={"Authorization": "Bearer nonsense"})
        assert auth("rejected", "invalid_token") == before + 1

    def test_refresh_token_misuse_is_counted_distinctly(self, gateway):
        gateway.post("/auth/register", json=CREDENTIALS)
        tokens = gateway.post("/auth/login", json=CREDENTIALS).json()

        before = auth("rejected", "wrong_token_type")
        gateway.get(
            "/api/protected", headers={"Authorization": f"Bearer {tokens['refresh_token']}"}
        )
        assert auth("rejected", "wrong_token_type") == before + 1

    def test_revoked_token_is_counted_distinctly(self, gateway, auth_header):
        """Revocations rising means logout is working, which is a different
        story from tokens simply expiring."""
        gateway.post("/auth/logout", headers=auth_header)

        before = auth("rejected", "token_revoked")
        gateway.get("/api/protected", headers=auth_header)
        assert auth("rejected", "token_revoked") == before + 1

    def test_open_routes_record_no_auth_decision(self, gateway):
        before = auth("accepted")
        gateway.get("/api/orders")
        assert auth("accepted") == before


class TestExposedInScrape:
    def test_decision_metrics_appear_in_the_scrape(self, gateway, auth_header):
        gateway.get("/api/limited")
        gateway.get("/api/protected", headers=auth_header)

        body = gateway.get("/metrics").text
        assert "gateway_rate_limit_decisions_total" in body
        assert "gateway_auth_attempts_total" in body
