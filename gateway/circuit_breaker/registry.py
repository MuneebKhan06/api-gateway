"""One breaker per upstream service.

Breakers are keyed by upstream, not by route. If service A serves three routes
and one of them starts failing, all three are suspect: the failing route is
evidence about the process behind it, not about that one path. Opening the
breaker for the whole service is the point.

The registry itself holds no breaker state. That is all in Redis, shared
across gateway instances. What this caches is the store objects, so a request
does not re-register the Lua scripts every time.
"""

import logging

import redis.asyncio as redis

from gateway.circuit_breaker.breaker import BreakerConfig, BreakerSnapshot, BreakerState
from gateway.circuit_breaker.store import BreakerStore
from gateway.metrics.prometheus import (
    observe_breaker_state,
    observe_breaker_transition,
)
from gateway.schemas.gateway import RouteConfig

logger = logging.getLogger(__name__)


class CircuitBreakerRegistry:
    def __init__(self, client: redis.Redis, config: BreakerConfig) -> None:
        self._redis = client
        self._config = config
        self._stores: dict[str, BreakerStore] = {}

    @property
    def config(self) -> BreakerConfig:
        return self._config

    def store_for(self, upstream: str) -> BreakerStore:
        store = self._stores.get(upstream)
        if store is None:
            store = BreakerStore(self._redis, self._config)
            self._stores[upstream] = store
            logger.debug("Created breaker store for %s", upstream)
        return store

    async def allow_request(self, upstream: str) -> tuple[bool, BreakerState]:
        return await self.store_for(upstream).allow_request(upstream)

    async def record_success(self, upstream: str) -> BreakerState:
        before = (await self.snapshot(upstream)).state
        state = await self.store_for(upstream).record_success(upstream)
        self._observe(upstream, before, state)
        return state

    async def record_failure(self, upstream: str) -> BreakerState:
        before = (await self.snapshot(upstream)).state
        state = await self.store_for(upstream).record_failure(upstream)
        if state is BreakerState.OPEN and before is not BreakerState.OPEN:
            logger.warning("Circuit breaker for %s is now open", upstream)
        self._observe(upstream, before, state)
        return state

    @staticmethod
    def _observe(upstream: str, before: BreakerState, after: BreakerState) -> None:
        """Keep the gauge current, and count only genuine changes.

        Counting every call would make the transition counter a duplicate of
        the request rate; it is only interesting when the state actually
        moved."""
        if before is after:
            observe_breaker_state(upstream, after.value)
        else:
            observe_breaker_transition(upstream, after.value)

    async def snapshot(self, upstream: str) -> BreakerSnapshot:
        return await self.store_for(upstream).snapshot(upstream)

    async def reset(self, upstream: str) -> None:
        await self.store_for(upstream).reset(upstream)

    async def trip(self, upstream: str) -> None:
        await self.store_for(upstream).trip(upstream)

    async def states_for(
        self, routes: list[RouteConfig]
    ) -> dict[str, BreakerState | None]:
        """Current state of every breaker the route table refers to.

        Routes with the breaker disabled are skipped rather than reported as
        closed, because "closed" would suggest a breaker that is watching. An
        upstream whose state could not be read maps to None.
        """
        upstreams = {
            route.name
            for route in routes
            if route.circuit_breaker and route.upstream is not None
        }

        states: dict[str, BreakerState] = {}
        for upstream in sorted(upstreams):
            try:
                states[upstream] = (await self.snapshot(upstream)).state
            except Exception as exc:
                # Introspection should degrade rather than fail. A Redis blip
                # makes the breaker state unknown, which is worth reporting
                # honestly, but it is not a reason to 500 the whole endpoint.
                logger.warning("Could not read breaker state for %s: %s", upstream, exc)
                states[upstream] = None
        return states

    def clear(self) -> None:
        """Drop cached stores after a route reload. Breaker state itself lives
        in Redis and deliberately survives, since a reload does not make a
        failing upstream healthy."""
        self._stores.clear()

    def __len__(self) -> int:
        return len(self._stores)


def build_registry(client: redis.Redis, settings) -> CircuitBreakerRegistry:
    config = BreakerConfig(
        failure_threshold=settings.breaker_failure_threshold,
        failure_window_seconds=settings.breaker_failure_window_seconds,
        recovery_timeout_seconds=settings.breaker_recovery_timeout_seconds,
        success_threshold=settings.breaker_success_threshold,
    )
    return CircuitBreakerRegistry(client, config)
