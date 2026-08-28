"""The Prometheus scrape endpoint."""

from prometheus_client import CONTENT_TYPE_LATEST


class TestExposition:
    def test_endpoint_is_served_by_the_gateway(self, gateway):
        """It used to fall through to the catch-all and report itself as an
        unimplemented gateway route."""
        response = gateway.get("/metrics")
        assert response.status_code == 200

    def test_content_type_is_the_prometheus_format(self, gateway):
        response = gateway.get("/metrics")
        assert response.headers["content-type"] == CONTENT_TYPE_LATEST

    def test_no_authentication_is_required(self, gateway):
        assert gateway.get("/metrics").status_code == 200

    def test_body_is_the_text_exposition_format(self, gateway):
        body = gateway.get("/metrics").text
        assert "# HELP" in body
        assert "# TYPE" in body

    def test_gateway_metrics_are_present(self, gateway):
        body = gateway.get("/metrics").text
        assert "gateway_requests_total" in body
        assert "gateway_request_duration_seconds" in body
        assert "gateway_errors_total" in body
        assert "gateway_requests_in_flight" in body


class TestValuesReflectTraffic:
    def test_traffic_shows_up_in_the_scrape(self, gateway):
        gateway.get("/api/orders")
        body = gateway.get("/metrics").text
        assert 'upstream="service-a"' in body

    def test_counters_increase_between_scrapes(self, gateway):
        def orders_count(text: str) -> float:
            for line in text.splitlines():
                if line.startswith("gateway_requests_total{") and 'upstream="service-a"' in line:
                    if 'status_code="200"' in line and 'method="GET"' in line:
                        return float(line.rsplit(" ", 1)[1])
            return 0.0

        before = orders_count(gateway.get("/metrics").text)
        gateway.get("/api/orders")
        after = orders_count(gateway.get("/metrics").text)
        assert after == before + 1

    def test_histogram_buckets_are_exposed(self, gateway):
        """Buckets are what let Prometheus aggregate a p95 across instances."""
        gateway.get("/api/orders")
        body = gateway.get("/metrics").text
        assert "gateway_request_duration_seconds_bucket" in body
        assert 'le="0.05"' in body


class TestScrapeIsCheap:
    def test_scraping_does_not_reset_counters(self, gateway):
        """Counters are monotonic; Prometheus derives rates from successive
        scrapes, so resetting them here would break every rate query."""
        gateway.get("/api/orders")
        first = gateway.get("/metrics").text
        second = gateway.get("/metrics").text
        assert 'upstream="service-a"' in first
        assert 'upstream="service-a"' in second

    def test_repeated_scrapes_do_not_inflate_request_counts(self, gateway):
        """Scraping must not count itself. Other gateway-owned endpoints share
        the same label legitimately, so this compares the count rather than
        asserting the label is absent."""
        from gateway.metrics.prometheus import REGISTRY

        def gateway_requests() -> float:
            value = REGISTRY.get_sample_value(
                "gateway_requests_total",
                {"upstream": "gateway", "method": "GET", "status_code": "200"},
            )
            return 0.0 if value is None else value

        before = gateway_requests()
        for _ in range(5):
            gateway.get("/metrics")
        assert gateway_requests() == before
