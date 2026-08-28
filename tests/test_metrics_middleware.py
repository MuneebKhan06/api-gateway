"""Metrics middleware, measured through the full gateway."""


from gateway.metrics.prometheus import REGISTRY
from upstream.service_a import main as service_a


def sample(name: str, **labels) -> float:
    value = REGISTRY.get_sample_value(name, labels)
    return 0.0 if value is None else value


def requests(upstream: str, method: str, status: int) -> float:
    return sample(
        "gateway_requests_total", upstream=upstream, method=method, status_code=str(status)
    )


def errors(upstream: str, method: str, error_type: str) -> float:
    return sample(
        "gateway_errors_total", upstream=upstream, method=method, error_type=error_type
    )


class TestRequestCounting:
    def test_a_proxied_request_is_counted(self, gateway):
        before = requests("service-a", "GET", 200)
        gateway.get("/api/orders")
        assert requests("service-a", "GET", 200) == before + 1

    def test_requests_are_labelled_by_upstream(self, gateway):
        before = requests("service-b", "GET", 200)
        gateway.get("/api/users")
        assert requests("service-b", "GET", 200) == before + 1

    def test_methods_are_counted_separately(self, gateway):
        before = requests("service-a", "POST", 201)
        gateway.post("/api/orders", json={"customer": "x", "total": 1})
        assert requests("service-a", "POST", 201) == before + 1

    def test_upstream_status_is_recorded(self, gateway):
        before = requests("service-a", "GET", 404)
        gateway.get("/api/orders/nope")
        assert requests("service-a", "GET", 404) == before + 1


class TestDuration:
    def test_duration_is_observed(self, gateway):
        before = sample(
            "gateway_request_duration_seconds_count", upstream="service-a", method="GET"
        )
        gateway.get("/api/orders")
        after = sample(
            "gateway_request_duration_seconds_count", upstream="service-a", method="GET"
        )
        assert after == before + 1

    def test_measured_time_is_positive_and_sane(self, gateway):
        before = sample(
            "gateway_request_duration_seconds_sum", upstream="service-a", method="GET"
        )
        gateway.get("/api/orders")
        after = sample(
            "gateway_request_duration_seconds_sum", upstream="service-a", method="GET"
        )
        elapsed = after - before
        assert 0 < elapsed < 5


class TestErrorCounting:
    def test_upstream_failure_is_counted_as_an_error(self, gateway):
        before = errors("service-a", "GET", "http_503")
        service_a._state["fail"] = True
        gateway.get("/api/orders")
        service_a._state["fail"] = False
        assert errors("service-a", "GET", "http_503") == before + 1

    def test_rate_limiting_is_counted_distinctly(self, gateway):
        before = errors("service-a", "GET", "rate_limited")
        for _ in range(5):
            gateway.get("/api/limited")
        assert errors("service-a", "GET", "rate_limited") > before

    def test_auth_failures_are_counted_distinctly(self, gateway):
        before = errors("service-a", "GET", "unauthorized")
        gateway.get("/api/protected")
        assert errors("service-a", "GET", "unauthorized") == before + 1

    def test_successful_requests_are_not_errors(self, gateway):
        before = errors("service-a", "GET", "http_500")
        gateway.get("/api/orders")
        assert errors("service-a", "GET", "http_500") == before

    def test_a_404_from_the_upstream_is_not_a_gateway_error(self, gateway):
        """The upstream answered correctly. A client asking for something that
        does not exist is not the gateway failing."""
        before = errors("service-a", "GET", "http_404")
        gateway.get("/api/orders/nope")
        assert errors("service-a", "GET", "http_404") == before


class TestLabelCardinality:
    def test_unmatched_paths_share_one_label(self, gateway):
        """Anyone can request any URL. Labelling by path would let a client
        create unbounded time series and exhaust the Prometheus server."""
        for path in ("/nope/a", "/nope/b", "/nope/c", "/totally/different"):
            gateway.get(path)

        upstream_labels = {
            s.labels["upstream"]
            for metric in REGISTRY.collect()
            for s in metric.samples
            if metric.name == "gateway_requests"
        }
        assert "unmatched" in upstream_labels
        assert not any(label.startswith("/nope") for label in upstream_labels)

    def test_no_label_contains_a_request_path(self, gateway):
        gateway.get("/api/orders/a1f0")
        for metric in REGISTRY.collect():
            for s in metric.samples:
                for value in s.labels.values():
                    assert "a1f0" not in value


class TestScrapeIsNotMeasured:
    def test_metrics_endpoint_does_not_count_itself(self, gateway):
        """Scraping runs every few seconds forever and would drown real
        traffic in the metrics it is reporting."""
        before = requests("gateway", "GET", 200)
        gateway.get("/metrics")
        assert requests("gateway", "GET", 200) == before


class TestInFlight:
    def test_gauge_returns_to_zero_after_a_request(self, gateway):
        gateway.get("/api/orders")
        assert sample("gateway_requests_in_flight", upstream="service-a") == 0

    def test_gauge_returns_to_zero_even_when_the_upstream_fails(self, gateway):
        service_a._state["fail"] = True
        gateway.get("/api/orders")
        service_a._state["fail"] = False
        assert sample("gateway_requests_in_flight", upstream="service-a") == 0
