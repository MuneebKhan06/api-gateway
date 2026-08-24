import httpx
import pytest

from gateway.proxy import (
    HOP_BY_HOP_HEADERS,
    ReverseProxy,
    UpstreamTimeout,
    UpstreamUnavailable,
    build_target,
    filter_request_headers,
    filter_response_headers,
)
from gateway.schemas.gateway import RouteConfig


def route(**overrides) -> RouteConfig:
    base = {"path_prefix": "/api/orders", "upstream": "http://service-a:8001"}
    return RouteConfig(**{**base, **overrides})


class TestBuildTarget:
    def test_strips_the_prefix_by_default(self):
        target = build_target(route(), "/api/orders/42")
        assert target.url == "http://service-a:8001/42"

    def test_bare_prefix_becomes_root(self):
        assert build_target(route(), "/api/orders").url == "http://service-a:8001/"

    def test_keeps_prefix_when_strip_is_off(self):
        target = build_target(route(strip_prefix=False), "/api/orders/42")
        assert target.url == "http://service-a:8001/api/orders/42"

    def test_query_string_is_carried_over(self):
        target = build_target(route(), "/api/orders", "status=open&page=2")
        assert target.url.endswith("/?status=open&page=2")

    def test_trailing_slash_on_upstream_does_not_double_up(self):
        target = build_target(route(upstream="http://service-a:8001/"), "/api/orders/42")
        assert target.url == "http://service-a:8001/42"

    def test_timeout_comes_from_the_route(self):
        assert build_target(route(timeout_seconds=7), "/api/orders").timeout == 7

    def test_route_without_upstream_is_an_error(self):
        with pytest.raises(ValueError):
            build_target(RouteConfig(path_prefix="/health"), "/health")


class TestHeaderFiltering:
    def test_hop_by_hop_headers_are_dropped(self):
        incoming = {"connection": "keep-alive", "te": "trailers", "accept": "application/json"}
        cleaned = filter_request_headers(incoming, "service-a:8001")
        assert "connection" not in cleaned
        assert "te" not in cleaned
        assert cleaned["accept"] == "application/json"

    def test_host_is_rewritten_to_the_upstream(self):
        cleaned = filter_request_headers({"host": "gateway.local"}, "service-a:8001")
        assert cleaned["host"] == "service-a:8001"

    def test_authorization_survives(self):
        # The auth middleware may want the upstream to see the caller.
        cleaned = filter_request_headers({"authorization": "Bearer x"}, "service-a:8001")
        assert cleaned["authorization"] == "Bearer x"

    def test_response_content_length_is_dropped(self):
        headers = httpx.Headers({"content-length": "12", "content-type": "application/json"})
        cleaned = filter_response_headers(headers)
        assert "content-length" not in cleaned
        assert cleaned["content-type"] == "application/json"

    def test_every_listed_hop_header_is_removed_from_responses(self):
        headers = httpx.Headers({name: "x" for name in HOP_BY_HOP_HEADERS})
        assert filter_response_headers(headers) == {}


class TestReverseProxyLifecycle:
    @pytest.mark.asyncio
    async def test_client_is_unavailable_before_startup(self):
        proxy = ReverseProxy()
        with pytest.raises(RuntimeError):
            _ = proxy.client

    @pytest.mark.asyncio
    async def test_startup_and_shutdown(self):
        proxy = ReverseProxy(max_connections=4)
        await proxy.startup()
        assert proxy.client is not None
        await proxy.shutdown()
        with pytest.raises(RuntimeError):
            _ = proxy.client

    @pytest.mark.asyncio
    async def test_shutdown_is_safe_to_call_twice(self):
        proxy = ReverseProxy()
        await proxy.startup()
        await proxy.shutdown()
        await proxy.shutdown()
