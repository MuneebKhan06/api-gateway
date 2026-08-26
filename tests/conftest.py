"""Shared test fixtures.

The gateway talks to upstreams over httpx. Rather than binding real ports for
every test, `MockUpstreamTransport` routes outbound requests by hostname into
the mock upstream ASGI apps running in the same process. From the gateway's
point of view nothing changes: it still builds a real request, still gets a
real response, still streams the body back.
"""

import asyncio

import fakeredis.aioredis
import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.config import Settings
from gateway.db.connection import Database
from gateway.main import create_app
from gateway.proxy import ReverseProxy
from gateway.redis_client import RedisClient
from upstream.service_a import main as service_a
from upstream.service_b import main as service_b
from upstream.service_c import main as service_c

TEST_ROUTES = """
routes:
  - path_prefix: /api/orders
    upstream: http://service-a:8001
    strip_prefix: true
    timeout_seconds: 5
    auth_required: false
  - path_prefix: /api/users
    upstream: http://service-b:8002
    strip_prefix: true
    timeout_seconds: 5
    auth_required: false
  - path_prefix: /api/inventory
    upstream: http://service-c:8003
    strip_prefix: true
    timeout_seconds: 5
    auth_required: false
  - path_prefix: /api/protected
    upstream: http://service-a:8001
    strip_prefix: true
    timeout_seconds: 5
    auth_required: true
  - path_prefix: /api/limited
    upstream: http://service-a:8001
    strip_prefix: true
    auth_required: false
    rate_limit:
      algorithm: fixed_window
      requests: 3
      window_seconds: 60
  - path_prefix: /api/limited-bucket
    upstream: http://service-a:8001
    strip_prefix: true
    auth_required: false
    rate_limit:
      algorithm: token_bucket
      requests: 3
      window_seconds: 60
  - path_prefix: /api/limited-auth
    upstream: http://service-a:8001
    strip_prefix: true
    auth_required: true
    rate_limit:
      algorithm: fixed_window
      requests: 3
      window_seconds: 60
  - path_prefix: /api/raw
    upstream: http://service-a:8001
    strip_prefix: false
    auth_required: false
  - path_prefix: /api/slow
    upstream: http://service-a:8001
    strip_prefix: true
    timeout_seconds: 0.25
    auth_required: false
  - path_prefix: /api/missing
    upstream: http://nowhere:9999
    strip_prefix: true
    auth_required: false
  - path_prefix: /health
    upstream: null
    auth_required: false
  - path_prefix: /auth
    upstream: null
    auth_required: false
  - path_prefix: /metrics
    upstream: null
    auth_required: false
"""


class MockUpstreamTransport(httpx.AsyncBaseTransport):
    """Dispatch outbound requests to in-process ASGI apps by hostname.

    An unknown host raises ConnectError, which is exactly what httpx does for
    a host that does not resolve. That is how the unreachable-upstream path
    gets tested.

    Timeouts have to be enforced here by hand. A real transport gives up on a
    socket that goes quiet, but an in-process ASGI call just awaits the app
    for as long as it takes, so without this the timeout path is untestable.
    """

    def __init__(self, apps: dict[str, object]) -> None:
        self._transports = {
            host: httpx.ASGITransport(app=app) for host, app in apps.items()
        }

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        transport = self._transports.get(request.url.host)
        if transport is None:
            raise httpx.ConnectError(
                f"nodename nor servname provided: {request.url.host}", request=request
            )

        read_timeout = (request.extensions.get("timeout") or {}).get("read")
        if read_timeout is None:
            return await transport.handle_async_request(request)

        try:
            return await asyncio.wait_for(
                transport.handle_async_request(request), timeout=read_timeout
            )
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise httpx.ReadTimeout(f"read timed out after {read_timeout}s", request=request) from exc


@pytest.fixture
def upstream_apps():
    """Reset fault injection between tests so state does not leak."""
    modules = (service_a, service_b, service_c)
    for module in modules:
        module._state["fail"] = False
        module._state["latency_ms"] = 0
    yield {
        "service-a": service_a.app,
        "service-b": service_b.app,
        "service-c": service_c.app,
    }
    for module in modules:
        module._state["fail"] = False
        module._state["latency_ms"] = 0


@pytest.fixture
def routes_file(tmp_path):
    path = tmp_path / "routes.yaml"
    path.write_text(TEST_ROUTES)
    return path


class FakeRedisClient(RedisClient):
    """RedisClient backed by fakeredis.

    Subclassed rather than mocked so the app still calls startup, shutdown and
    ping exactly as it would against a real server.
    """

    def __init__(self) -> None:
        super().__init__(url="redis://fake", max_connections=10)

    async def startup(self) -> None:
        self._client = fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def settings(routes_file, tmp_path):
    # A file backed SQLite database rather than :memory:, because an in-memory
    # one is scoped to a single connection and the app opens its own.
    return Settings(
        routes_file=str(routes_file),
        environment="test",
        jwt_secret_key="test-secret-key",
        database_url=f"sqlite+aiosqlite:///{tmp_path}/gateway-test.db",
    )


async def _create_schema(url: str) -> None:
    database = Database(url)
    await database.startup()
    await database.create_all()
    await database.shutdown()


@pytest.fixture
def gateway(settings, upstream_apps):
    """A fully wired gateway: real request path, fake backing services."""
    # Tables are created before the app boots so its engine finds a schema.
    asyncio.run(_create_schema(settings.database_url))

    proxy = ReverseProxy(transport=MockUpstreamTransport(upstream_apps))
    app = create_app(
        settings,
        proxy=proxy,
        redis_client=FakeRedisClient(),
        database=Database(settings.database_url),
    )

    with TestClient(app) as client:
        yield client


@pytest.fixture(scope="session", autouse=True)
def lua_scripting_is_available():
    """Guard against the limiters silently failing open in tests.

    Every rate limiter runs as a Lua script, and fakeredis only executes Lua
    when lupa is installed. Without it EVALSHA raises, the limiters take their
    fail-open path, and the tests pass while testing nothing at all. Better to
    stop the whole run with a clear reason.
    """
    import fakeredis

    client = fakeredis.FakeStrictRedis()
    try:
        client.eval("return 1", 0)
    except Exception as exc:
        pytest.fail(
            "fakeredis cannot execute Lua scripts, so the rate limiters would "
            f"silently fail open instead of being tested. Install lupa. ({exc})"
        )
    finally:
        client.close()
