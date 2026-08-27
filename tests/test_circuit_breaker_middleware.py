"""Circuit breaker behaviour through the full gateway.

The fixture threshold is 3 failures, recovery 30s, 2 successes to close.
service-a's fault injection is what makes it fail on demand.
"""

import pytest

from gateway.circuit_breaker.breaker import BreakerState
from gateway.middleware.circuit_breaker import BREAKER_STATE_HEADER
from upstream.service_a import main as service_a


def trip_breaker(gateway, path="/api/orders", times=3):
    """Drive enough upstream failures to open the breaker."""
    service_a._state["fail"] = True
    for _ in range(times):
        gateway.get(path)
    service_a._state["fail"] = False


class TestClosedBreaker:
    def test_healthy_upstream_passes_through(self, gateway):
        response = gateway.get("/api/orders")
        assert response.status_code == 200

    def test_responses_report_the_breaker_state(self, gateway):
        response = gateway.get("/api/orders")
        assert response.headers[BREAKER_STATE_HEADER] == BreakerState.CLOSED.value

    def test_failures_below_the_threshold_keep_it_closed(self, gateway):
        service_a._state["fail"] = True
        for _ in range(2):
            assert gateway.get("/api/orders").status_code == 503
        service_a._state["fail"] = False

        # Still passing traffic, because the threshold was not reached.
        assert gateway.get("/api/orders").status_code == 200


class TestOpening:
    def test_enough_failures_open_the_breaker(self, gateway):
        trip_breaker(gateway)
        snapshot_state = gateway.app.state.circuit_breakers
        assert snapshot_state is not None

        # The upstream is healthy again, but the breaker is not consulting it.
        response = gateway.get("/api/orders")
        assert response.status_code == 503
        assert response.json()["error"] == "circuit_open"

    def test_open_breaker_does_not_call_the_upstream(self, gateway):
        """The point of the pattern: the upstream gets a rest, and the client
        gets an answer immediately instead of waiting out a timeout."""
        trip_breaker(gateway)

        response = gateway.get("/api/orders")
        assert response.status_code == 503
        # An upstream response would have carried this header.
        assert "x-upstream-service" not in response.headers

    def test_refusal_tells_the_client_when_to_retry(self, gateway):
        trip_breaker(gateway)
        response = gateway.get("/api/orders")
        assert int(response.headers["retry-after"]) == 30

    def test_refusal_carries_the_request_id(self, gateway):
        trip_breaker(gateway)
        response = gateway.get("/api/orders", headers={"X-Request-ID": "trace-5"})
        assert response.json()["request_id"] == "trace-5"

    def test_timeouts_count_as_failures(self, gateway):
        """A slow upstream is as bad as a broken one, so 504s open the breaker
        just as 503s do."""
        service_a._state["latency_ms"] = 800  # /api/slow times out at 0.25s
        for _ in range(3):
            assert gateway.get("/api/slow").status_code == 504
        service_a._state["latency_ms"] = 0

        assert gateway.get("/api/slow").status_code == 503

    def test_unreachable_upstream_opens_the_breaker(self, gateway):
        for _ in range(3):
            assert gateway.get("/api/missing").status_code == 502
        assert gateway.get("/api/missing").json()["error"] == "circuit_open"


class TestClientErrorsDoNotOpenIt:
    def test_404s_do_not_count(self, gateway):
        """A client asking for things that do not exist is not evidence the
        service is unwell, and must not let any client take it offline."""
        for _ in range(10):
            assert gateway.get("/api/orders/no-such-order").status_code == 404

        assert gateway.get("/api/orders").status_code == 200

    def test_409s_do_not_count(self, gateway):
        for _ in range(10):
            gateway.patch("/api/inventory/SKU-1003", json={"delta": -1})
        assert gateway.get("/api/inventory").status_code == 200

    def test_a_success_resets_the_failure_count(self, gateway):
        service_a._state["fail"] = True
        for _ in range(2):
            gateway.get("/api/orders")
        service_a._state["fail"] = False

        assert gateway.get("/api/orders").status_code == 200

        # Two more failures should not be enough, since the count was cleared.
        service_a._state["fail"] = True
        for _ in range(2):
            gateway.get("/api/orders")
        service_a._state["fail"] = False

        assert gateway.get("/api/orders").status_code == 200


class TestPerUpstreamScope:
    def test_one_service_opening_does_not_affect_another(self, gateway):
        trip_breaker(gateway)
        assert gateway.get("/api/orders").status_code == 503
        # service-b is untouched.
        assert gateway.get("/api/users").status_code == 200

    def test_every_route_of_a_failing_service_is_refused(self, gateway):
        """A failing route is evidence about the process behind it, so the
        service's other routes are refused too."""
        trip_breaker(gateway, path="/api/orders")
        assert gateway.get("/api/protected").status_code in (401, 503)
        assert gateway.get("/api/raw").status_code == 503


class TestRecovery:
    @pytest.fixture
    def opened(self, gateway):
        trip_breaker(gateway)
        return gateway

    async def _advance(self, gateway, seconds):
        """Move the breaker's clock rather than sleeping for 30 seconds."""
        registry = gateway.app.state.circuit_breakers
        store = registry.store_for("service-a")
        snapshot = await store.snapshot("service-a")
        if snapshot.opened_at is not None:
            await store._redis.hset(
                store.key("service-a"), "opened_at", snapshot.opened_at - seconds
            )

    def test_still_refused_before_the_timeout(self, opened):
        assert opened.get("/api/orders").status_code == 503

    def test_trial_request_allowed_after_the_timeout(self, opened):
        opened.portal.call(self._advance, opened, 31)
        # Upstream is healthy now, so the trial succeeds.
        assert opened.get("/api/orders").status_code == 200

    def test_two_successes_close_it(self, opened):
        opened.portal.call(self._advance, opened, 31)
        assert opened.get("/api/orders").status_code == 200
        assert opened.get("/api/orders").status_code == 200

        # Fully closed, so traffic flows without further trials.
        for _ in range(3):
            assert opened.get("/api/orders").status_code == 200

    def test_a_failed_trial_reopens_it(self, opened):
        opened.portal.call(self._advance, opened, 31)

        service_a._state["fail"] = True
        assert opened.get("/api/orders").status_code == 503
        service_a._state["fail"] = False

        # Back to refusing without calling the upstream.
        response = opened.get("/api/orders")
        assert response.json()["error"] == "circuit_open"


class TestDisabledBreaker:
    def test_routes_with_the_breaker_off_are_never_refused(self, gateway):
        """/health has no upstream and no breaker."""
        service_a._state["fail"] = True
        for _ in range(10):
            assert gateway.get("/health").status_code == 200
        service_a._state["fail"] = False


class TestUnknownPaths:
    def test_unknown_path_is_a_404_not_a_503(self, gateway):
        for _ in range(10):
            response = gateway.get("/no/such/route")
        assert response.status_code == 404
