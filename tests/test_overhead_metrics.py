"""Gateway overhead: end to end time minus time spent waiting on the upstream.

Without splitting these, a slow upstream and a slow gateway look identical on
a latency graph, and the one number the gateway is actually responsible for is
invisible.
"""

from gateway.metrics.prometheus import REGISTRY
from upstream.service_a import main as service_a


def sample(name: str, **labels) -> float:
    value = REGISTRY.get_sample_value(name, labels)
    return 0.0 if value is None else value


def upstream_sum(upstream="service-a", method="GET") -> float:
    return sample("gateway_upstream_duration_seconds_sum", upstream=upstream, method=method)


def upstream_count(upstream="service-a", method="GET") -> float:
    return sample("gateway_upstream_duration_seconds_count", upstream=upstream, method=method)


def total_sum(upstream="service-a", method="GET") -> float:
    return sample("gateway_request_duration_seconds_sum", upstream=upstream, method=method)


class TestUpstreamTiming:
    def test_a_proxied_request_records_upstream_time(self, gateway):
        before = upstream_count()
        gateway.get("/api/orders")
        assert upstream_count() == before + 1

    def test_upstream_time_is_positive(self, gateway):
        before = upstream_sum()
        gateway.get("/api/orders")
        assert upstream_sum() > before

    def test_methods_are_tracked_separately(self, gateway):
        before = upstream_count(method="POST")
        gateway.post("/api/orders", json={"customer": "x", "total": 1})
        assert upstream_count(method="POST") == before + 1

    def test_upstreams_are_tracked_separately(self, gateway):
        before = upstream_count(upstream="service-b")
        gateway.get("/api/users")
        assert upstream_count(upstream="service-b") == before + 1


class TestOverheadIsDerivable:
    def test_upstream_time_is_within_total_time(self, gateway):
        """The gateway cannot spend less time than the upstream it waited on,
        so the difference is its own overhead and must not be negative."""
        total_before = total_sum()
        upstream_before = upstream_sum()

        for _ in range(5):
            gateway.get("/api/orders")

        total_elapsed = total_sum() - total_before
        upstream_elapsed = upstream_sum() - upstream_before

        assert upstream_elapsed > 0
        assert total_elapsed >= upstream_elapsed

    def test_a_slow_upstream_shows_up_as_upstream_time_not_overhead(self, gateway):
        """The distinction that makes the split worth having."""
        service_a._state["latency_ms"] = 120
        total_before = total_sum()
        upstream_before = upstream_sum()

        gateway.get("/api/orders")
        service_a._state["latency_ms"] = 0

        total_elapsed = total_sum() - total_before
        upstream_elapsed = upstream_sum() - upstream_before
        overhead = total_elapsed - upstream_elapsed

        # Most of the request was the upstream being slow, not the gateway.
        assert upstream_elapsed >= 0.1
        assert overhead < upstream_elapsed


class TestFailuresAreStillTimed:
    def test_a_timeout_records_upstream_time(self, gateway):
        """Dropping failures would make an upstream look faster the worse it
        got, which is exactly backwards."""
        service_a._state["latency_ms"] = 800  # /api/slow times out at 0.25s
        before = upstream_count()
        gateway.get("/api/slow")
        service_a._state["latency_ms"] = 0
        assert upstream_count() == before + 1

    def test_an_unreachable_upstream_records_time(self, gateway):
        before = upstream_count(upstream="nowhere")
        gateway.get("/api/missing")
        assert upstream_count(upstream="nowhere") == before + 1


class TestNotRecordedWhenNoCallHappens:
    def test_a_refused_request_records_no_upstream_time(self, gateway):
        """An open breaker means nothing was sent, so there is no upstream
        latency to attribute."""
        service_a._state["fail"] = True
        for _ in range(3):
            gateway.get("/api/orders")
        service_a._state["fail"] = False

        before = upstream_count()
        gateway.get("/api/orders")  # refused by the breaker
        assert upstream_count() == before

    def test_a_rate_limited_request_records_no_upstream_time(self, gateway):
        before = upstream_count()
        for _ in range(5):
            gateway.get("/api/limited")
        # Three got through to the upstream, the rest were rejected first.
        assert upstream_count() == before + 3


class TestExposedInScrape:
    def test_upstream_duration_appears_in_the_scrape(self, gateway):
        gateway.get("/api/orders")
        body = gateway.get("/metrics").text
        assert "gateway_upstream_duration_seconds" in body
        assert "gateway_upstream_duration_seconds_bucket" in body
