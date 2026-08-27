import fakeredis.aioredis
import pytest

from gateway.circuit_breaker.breaker import BreakerConfig, BreakerState
from gateway.circuit_breaker.registry import CircuitBreakerRegistry, build_registry
from gateway.config import Settings
from gateway.schemas.gateway import RouteConfig


@pytest.fixture
async def redis_client():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
def registry(redis_client):
    return CircuitBreakerRegistry(
        redis_client,
        BreakerConfig(failure_threshold=3, recovery_timeout_seconds=30, success_threshold=2),
    )


class TestStoreCaching:
    def test_same_upstream_reuses_one_store(self, registry):
        assert registry.store_for("service-a") is registry.store_for("service-a")
        assert len(registry) == 1

    def test_different_upstreams_get_their_own(self, registry):
        registry.store_for("service-a")
        registry.store_for("service-b")
        assert len(registry) == 2

    def test_clear_drops_cached_stores(self, registry):
        registry.store_for("service-a")
        registry.clear()
        assert len(registry) == 0

    async def test_clearing_the_cache_does_not_clear_breaker_state(self, registry):
        """A route reload does not make a failing upstream healthy, so the
        state in Redis has to survive it."""
        for _ in range(3):
            await registry.record_failure("service-a")
        registry.clear()

        assert (await registry.snapshot("service-a")).state is BreakerState.OPEN


class TestPerUpstreamScope:
    async def test_one_upstream_opening_does_not_affect_another(self, registry):
        for _ in range(3):
            await registry.record_failure("service-a")

        assert (await registry.allow_request("service-a"))[0] is False
        assert (await registry.allow_request("service-b"))[0] is True

    async def test_all_routes_of_one_service_share_a_breaker(self, registry):
        """A failing route is evidence about the process behind it, so every
        route of that service is suspect."""
        routes = [
            RouteConfig(path_prefix="/api/orders", upstream="http://service-a:8001"),
            RouteConfig(path_prefix="/api/carts", upstream="http://service-a:8001"),
        ]
        assert routes[0].name == routes[1].name == "service-a"

        for _ in range(3):
            await registry.record_failure(routes[0].name)
        assert (await registry.allow_request(routes[1].name))[0] is False


class TestStatesForRoutes:
    async def test_reports_each_distinct_upstream_once(self, registry):
        routes = [
            RouteConfig(path_prefix="/api/orders", upstream="http://service-a:8001"),
            RouteConfig(path_prefix="/api/carts", upstream="http://service-a:8001"),
            RouteConfig(path_prefix="/api/users", upstream="http://service-b:8002"),
        ]
        states = await registry.states_for(routes)
        assert set(states) == {"service-a", "service-b"}

    async def test_reflects_an_open_breaker(self, registry):
        routes = [RouteConfig(path_prefix="/api/orders", upstream="http://service-a:8001")]
        for _ in range(3):
            await registry.record_failure("service-a")

        assert (await registry.states_for(routes))["service-a"] is BreakerState.OPEN

    async def test_routes_with_the_breaker_off_are_omitted(self, registry):
        """Reporting "closed" would imply a breaker is watching when none is."""
        routes = [
            RouteConfig(
                path_prefix="/api/orders",
                upstream="http://service-a:8001",
                circuit_breaker=False,
            )
        ]
        assert await registry.states_for(routes) == {}

    async def test_gateway_owned_routes_are_omitted(self, registry):
        routes = [RouteConfig(path_prefix="/health", upstream=None)]
        assert await registry.states_for(routes) == {}


class TestBuildFromSettings:
    def test_thresholds_come_from_settings(self):
        settings = Settings(
            breaker_failure_threshold=7,
            breaker_failure_window_seconds=120,
            breaker_recovery_timeout_seconds=45,
            breaker_success_threshold=3,
        )
        registry = build_registry(None, settings)
        assert registry.config.failure_threshold == 7
        assert registry.config.failure_window_seconds == 120
        assert registry.config.recovery_timeout_seconds == 45
        assert registry.config.success_threshold == 3
