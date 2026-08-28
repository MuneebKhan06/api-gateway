"""Metric definitions and their labelling rules."""

import pytest
from prometheus_client import REGISTRY as DEFAULT_REGISTRY

from gateway.metrics.prometheus import (
    LATENCY_BUCKETS,
    REGISTRY,
    classify_error,
    observe_error,
    observe_request,
)


def sample(metric_name: str, **labels) -> float:
    value = REGISTRY.get_sample_value(metric_name, labels)
    return 0.0 if value is None else value


class TestRegistry:
    def test_metrics_are_not_on_the_global_registry(self):
        """The default registry is process wide and shared with anything else
        that imports prometheus_client, which makes tests order dependent and
        re-registration an error."""
        names = {metric.name for metric in DEFAULT_REGISTRY.collect()}
        assert "gateway_requests_total" not in names

    def test_all_gateway_metrics_are_registered(self):
        names = {metric.name for metric in REGISTRY.collect()}
        assert "gateway_requests" in names
        assert "gateway_errors" in names
        assert "gateway_request_duration_seconds" in names
        assert "gateway_requests_in_flight" in names


class TestRequestObservation:
    def test_counts_a_request(self):
        before = sample(
            "gateway_requests_total", upstream="svc", method="GET", status_code="200"
        )
        observe_request("svc", "GET", 200, 0.01)
        after = sample(
            "gateway_requests_total", upstream="svc", method="GET", status_code="200"
        )
        assert after == before + 1

    def test_status_codes_are_counted_separately(self):
        observe_request("svc-b", "GET", 200, 0.01)
        observe_request("svc-b", "GET", 500, 0.01)

        assert sample(
            "gateway_requests_total", upstream="svc-b", method="GET", status_code="200"
        ) == 1
        assert sample(
            "gateway_requests_total", upstream="svc-b", method="GET", status_code="500"
        ) == 1

    def test_upstreams_are_counted_separately(self):
        observe_request("svc-c", "GET", 200, 0.01)
        observe_request("svc-d", "GET", 200, 0.01)
        assert sample(
            "gateway_requests_total", upstream="svc-c", method="GET", status_code="200"
        ) == 1

    def test_duration_is_recorded(self):
        observe_request("svc-e", "POST", 200, 0.03)
        assert sample(
            "gateway_request_duration_seconds_count", upstream="svc-e", method="POST"
        ) == 1
        assert sample(
            "gateway_request_duration_seconds_sum", upstream="svc-e", method="POST"
        ) == pytest.approx(0.03)


class TestErrorObservation:
    def test_counts_an_error(self):
        observe_error("svc-f", "GET", "circuit_open")
        assert sample(
            "gateway_errors_total", upstream="svc-f", method="GET", error_type="circuit_open"
        ) == 1

    def test_error_types_are_counted_separately(self):
        observe_error("svc-g", "GET", "circuit_open")
        observe_error("svc-g", "GET", "upstream_timeout")
        assert sample(
            "gateway_errors_total", upstream="svc-g", method="GET", error_type="circuit_open"
        ) == 1
        assert sample(
            "gateway_errors_total",
            upstream="svc-g",
            method="GET",
            error_type="upstream_timeout",
        ) == 1


class TestErrorClassification:
    def test_successes_are_not_errors(self):
        assert classify_error(200) is None
        assert classify_error(201) is None
        assert classify_error(404) is None

    def test_server_errors_are_named_by_status(self):
        assert classify_error(500) == "http_500"
        assert classify_error(502) == "http_502"

    def test_rate_limiting_is_distinguishable(self):
        assert classify_error(429) == "rate_limited"

    def test_auth_failures_are_distinguishable(self):
        assert classify_error(401) == "unauthorized"
        assert classify_error(403) == "unauthorized"

    def test_gateway_error_code_wins_over_the_status(self):
        """circuit_open and upstream_unavailable are both 503 but mean very
        different things to whoever is on call."""
        assert classify_error(503, "circuit_open") == "circuit_open"
        assert classify_error(503, "upstream_unavailable") == "upstream_unavailable"


class TestBuckets:
    def test_buckets_cover_sub_millisecond_to_thirty_seconds(self):
        assert min(LATENCY_BUCKETS) <= 0.001
        assert max(LATENCY_BUCKETS) >= 30.0

    def test_buckets_are_dense_where_a_gateway_actually_sits(self):
        """A healthy proxied request is single digit milliseconds, so the
        library default of ten buckets topping out at 10s is too coarse."""
        fast = [b for b in LATENCY_BUCKETS if b <= 0.1]
        assert len(fast) >= 6

    def test_buckets_are_ascending(self):
        assert list(LATENCY_BUCKETS) == sorted(LATENCY_BUCKETS)


class TestHistogramNotSummary:
    def test_duration_exposes_buckets(self):
        """Buckets are what let Prometheus compute a p95 across several
        gateway instances. A summary computes quantiles in process and cannot
        be aggregated."""
        observe_request("svc-h", "GET", 200, 0.01)
        names = {
            sample.name
            for metric in REGISTRY.collect()
            for sample in metric.samples
            if metric.name == "gateway_request_duration_seconds"
        }
        assert "gateway_request_duration_seconds_bucket" in names
