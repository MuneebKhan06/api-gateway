"""Health check results exported as metrics.

/health answers whoever asks. Exporting the same answer means an upstream
going unhealthy can be graphed against traffic and error rate at that moment,
and alerted on without something polling the endpoint.
"""

from gateway.metrics.prometheus import REGISTRY
from upstream.service_a import main as service_a


def sample(name: str, **labels) -> float:
    value = REGISTRY.get_sample_value(name, labels)
    return -1.0 if value is None else value


def upstream(name: str) -> float:
    return sample("gateway_upstream_health", upstream=name)


def dependency(name: str) -> float:
    return sample("gateway_dependency_health", dependency=name)


class TestUpstreamHealth:
    def test_healthy_upstreams_report_one(self, gateway):
        gateway.get("/health")
        assert upstream("service-a") == 1
        assert upstream("service-b") == 1

    def test_a_failing_upstream_reports_zero(self, gateway):
        service_a._state["fail"] = True
        gateway.get("/health")
        service_a._state["fail"] = False
        assert upstream("service-a") == 0

    def test_an_unreachable_upstream_reports_zero(self, gateway):
        gateway.get("/health")
        # /api/missing points at a host that does not resolve.
        assert upstream("nowhere") == 0

    def test_recovery_returns_the_gauge_to_one(self, gateway):
        service_a._state["fail"] = True
        gateway.get("/health")
        assert upstream("service-a") == 0

        service_a._state["fail"] = False
        gateway.get("/health")
        assert upstream("service-a") == 1

    def test_an_open_breaker_reports_zero(self, gateway):
        """A service the gateway has stopped calling is unusable, whether or
        not it was actually contacted."""
        service_a._state["fail"] = True
        for _ in range(3):
            gateway.get("/api/orders")
        service_a._state["fail"] = False

        gateway.get("/health")
        assert upstream("service-a") == 0


class TestDependencyHealth:
    def test_connected_dependencies_report_one(self, gateway):
        gateway.get("/health")
        assert dependency("redis") == 1
        assert dependency("database") == 1

    def test_a_dead_dependency_reports_zero(self, gateway):
        gateway.app.state.redis._client = None
        gateway.get("/health")
        assert dependency("redis") == 0
        assert dependency("database") == 1


class TestExposedInScrape:
    def test_health_gauges_appear_in_the_scrape(self, gateway):
        gateway.get("/health")
        body = gateway.get("/metrics").text
        assert "gateway_upstream_health" in body
        assert "gateway_dependency_health" in body
