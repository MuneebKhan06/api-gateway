"""Circuit breaker metrics."""

from gateway.metrics.prometheus import REGISTRY, STATE_VALUES
from upstream.service_a import main as service_a


def sample(name: str, **labels) -> float:
    value = REGISTRY.get_sample_value(name, labels)
    return 0.0 if value is None else value


def state(upstream: str) -> float:
    return sample("gateway_circuit_breaker_state", upstream=upstream)


def transitions(upstream: str, to_state: str) -> float:
    return sample(
        "gateway_circuit_breaker_transitions_total", upstream=upstream, to_state=to_state
    )


def rejections(upstream: str) -> float:
    return sample("gateway_circuit_breaker_rejections_total", upstream=upstream)


def trip(gateway, times=3):
    service_a._state["fail"] = True
    for _ in range(times):
        gateway.get("/api/orders")
    service_a._state["fail"] = False


class TestStateEncoding:
    def test_states_map_to_distinct_numbers(self):
        """Prometheus only stores numbers, so the state has to be encoded."""
        assert STATE_VALUES == {"closed": 0, "open": 1, "half_open": 2}

    def test_healthy_traffic_reports_closed(self, gateway):
        gateway.get("/api/orders")
        assert state("service-a") == STATE_VALUES["closed"]

    def test_an_open_breaker_reports_open(self, gateway):
        trip(gateway)
        assert state("service-a") == STATE_VALUES["open"]

    def test_a_reset_returns_the_gauge_to_closed(self, gateway):
        trip(gateway)
        assert state("service-a") == STATE_VALUES["open"]

        gateway.post("/gateway/breakers/service-a/reset")
        gateway.get("/api/orders")
        assert state("service-a") == STATE_VALUES["closed"]

    def test_upstreams_are_tracked_separately(self, gateway):
        trip(gateway)
        gateway.get("/api/users")
        assert state("service-a") == STATE_VALUES["open"]
        assert state("service-b") == STATE_VALUES["closed"]


class TestTransitions:
    def test_opening_is_counted(self, gateway):
        before = transitions("service-a", "open")
        trip(gateway)
        assert transitions("service-a", "open") == before + 1

    def test_only_real_changes_are_counted(self, gateway):
        """Counting every call would make this a duplicate of the request
        rate. It is only interesting when the state actually moved."""
        gateway.get("/api/orders")
        before = transitions("service-a", "closed")
        for _ in range(5):
            gateway.get("/api/orders")
        assert transitions("service-a", "closed") == before

    def test_repeated_failures_while_open_do_not_recount(self, gateway):
        trip(gateway)
        before = transitions("service-a", "open")
        for _ in range(5):
            gateway.get("/api/orders")
        assert transitions("service-a", "open") == before


class TestRejections:
    def test_refused_requests_are_counted(self, gateway):
        trip(gateway)
        before = rejections("service-a")
        gateway.get("/api/orders")
        assert rejections("service-a") == before + 1

    def test_nothing_is_counted_while_closed(self, gateway):
        before = rejections("service-a")
        for _ in range(3):
            gateway.get("/api/orders")
        assert rejections("service-a") == before

    def test_rejections_show_the_traffic_the_upstream_was_spared(self, gateway):
        trip(gateway)
        before = rejections("service-a")
        for _ in range(10):
            gateway.get("/api/orders")
        assert rejections("service-a") == before + 10


class TestExposedInScrape:
    def test_breaker_metrics_appear_in_the_scrape(self, gateway):
        trip(gateway)
        gateway.get("/api/orders")

        body = gateway.get("/metrics").text
        assert "gateway_circuit_breaker_state" in body
        assert "gateway_circuit_breaker_transitions_total" in body
        assert "gateway_circuit_breaker_rejections_total" in body
