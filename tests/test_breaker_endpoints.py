"""Breaker introspection and manual control endpoints."""

from upstream.service_a import main as service_a


def trip(gateway, path="/api/orders", times=3):
    service_a._state["fail"] = True
    for _ in range(times):
        gateway.get(path)
    service_a._state["fail"] = False


class TestListing:
    def test_lists_every_upstream_with_a_breaker(self, gateway):
        response = gateway.get("/gateway/breakers")
        assert response.status_code == 200

        names = {entry["upstream"] for entry in response.json()}
        assert {"service-a", "service-b", "service-c"} <= names

    def test_healthy_upstreams_report_closed(self, gateway):
        entries = gateway.get("/gateway/breakers").json()
        assert all(entry["state"] == "closed" for entry in entries)

    def test_configured_thresholds_are_reported(self, gateway):
        entry = gateway.get("/gateway/breakers").json()[0]
        assert entry["failure_threshold"] == 3
        assert entry["recovery_timeout_seconds"] == 30

    def test_an_open_breaker_shows_up(self, gateway):
        trip(gateway)
        entries = {e["upstream"]: e for e in gateway.get("/gateway/breakers").json()}
        assert entries["service-a"]["state"] == "open"
        assert entries["service-a"]["opened_at"] is not None
        # Other services are unaffected.
        assert entries["service-b"]["state"] == "closed"

    def test_accumulating_failures_are_visible_before_opening(self, gateway):
        service_a._state["fail"] = True
        gateway.get("/api/orders")
        service_a._state["fail"] = False

        entries = {e["upstream"]: e for e in gateway.get("/gateway/breakers").json()}
        assert entries["service-a"]["failures"] == 1
        assert entries["service-a"]["state"] == "closed"


class TestSingleBreaker:
    def test_fetches_one_upstream(self, gateway):
        body = gateway.get("/gateway/breakers/service-a").json()
        assert body["upstream"] == "service-a"
        assert body["url"] == "http://service-a:8001"

    def test_unknown_upstream_is_a_404(self, gateway):
        response = gateway.get("/gateway/breakers/service-z")
        assert response.status_code == 404
        assert response.json()["error"] == "unknown_upstream"


class TestReset:
    def test_reset_closes_an_open_breaker(self, gateway):
        trip(gateway)
        assert gateway.get("/api/orders").status_code == 503

        response = gateway.post("/gateway/breakers/service-a/reset")
        assert response.status_code == 200

        # Traffic flows again without waiting out the recovery timeout.
        assert gateway.get("/api/orders").status_code == 200

    def test_reset_reports_the_new_state(self, gateway):
        trip(gateway)
        gateway.post("/gateway/breakers/service-a/reset")
        assert gateway.get("/gateway/breakers/service-a").json()["state"] == "closed"

    def test_reset_clears_accumulated_failures(self, gateway):
        service_a._state["fail"] = True
        for _ in range(2):
            gateway.get("/api/orders")
        service_a._state["fail"] = False

        gateway.post("/gateway/breakers/service-a/reset")
        assert gateway.get("/gateway/breakers/service-a").json()["failures"] == 0

    def test_resetting_an_unknown_upstream_is_a_404(self, gateway):
        assert gateway.post("/gateway/breakers/service-z/reset").status_code == 404


class TestTrip:
    def test_trip_opens_a_healthy_breaker(self, gateway):
        assert gateway.get("/api/orders").status_code == 200

        response = gateway.post("/gateway/breakers/service-a/trip")
        assert response.status_code == 200

        # The upstream is fine, but the gateway has stopped calling it.
        assert gateway.get("/api/orders").status_code == 503

    def test_tripped_breaker_reports_open(self, gateway):
        gateway.post("/gateway/breakers/service-a/trip")
        assert gateway.get("/gateway/breakers/service-a").json()["state"] == "open"

    def test_trip_only_affects_the_named_upstream(self, gateway):
        gateway.post("/gateway/breakers/service-a/trip")
        assert gateway.get("/api/users").status_code == 200

    def test_tripping_an_unknown_upstream_is_a_404(self, gateway):
        assert gateway.post("/gateway/breakers/service-z/trip").status_code == 404

    def test_a_tripped_breaker_can_be_reset(self, gateway):
        gateway.post("/gateway/breakers/service-a/trip")
        gateway.post("/gateway/breakers/service-a/reset")
        assert gateway.get("/api/orders").status_code == 200
