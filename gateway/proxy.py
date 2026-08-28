"""Reverse proxy.

Takes a matched route and an incoming request, forwards it to the upstream,
and streams the response back. This is the part a gateway is actually for;
everything else in the middleware chain decides whether we get this far.

Two things matter here and are easy to get wrong:

1. Hop-by-hop headers must not be forwarded. They describe a single TCP hop,
   not the end to end message, so passing them along corrupts connection
   handling. RFC 9110 lists them.
2. The response body is streamed rather than buffered. A 200 MB download
   through the gateway should not become 200 MB of gateway memory.
"""

import logging
import time
from dataclasses import dataclass

import httpx
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import StreamingResponse

from gateway.metrics.prometheus import observe_upstream_duration
from gateway.middleware.correlation import REQUEST_ID_HEADER, get_request_id
from gateway.schemas.gateway import RouteConfig

logger = logging.getLogger(__name__)

# Identity of the authenticated caller, handed to the upstream so it does not
# have to parse the JWT again. Named with the gateway's own prefix so there is
# no chance of colliding with something the upstream already uses.
USER_ID_HEADER = "x-gateway-user-id"
USER_EMAIL_HEADER = "x-gateway-user-email"
USER_ROLES_HEADER = "x-gateway-user-roles"

IDENTITY_HEADERS = frozenset({USER_ID_HEADER, USER_EMAIL_HEADER, USER_ROLES_HEADER})

# Headers that apply to one connection only and must be dropped at each hop.
HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


class UpstreamError(Exception):
    """Base for anything that went wrong talking to the upstream."""

    def __init__(self, upstream: str, message: str) -> None:
        self.upstream = upstream
        super().__init__(message)


class UpstreamTimeout(UpstreamError):
    """Upstream did not answer within the route's timeout."""


class UpstreamUnavailable(UpstreamError):
    """Could not open a connection to the upstream at all."""


@dataclass(frozen=True)
class ProxyTarget:
    url: str
    timeout: float


def build_target(route: RouteConfig, path: str, query: str = "") -> ProxyTarget:
    """Work out the absolute upstream URL for an incoming path."""
    if route.upstream is None:
        raise ValueError(f"route {route.path_prefix} has no upstream to proxy to")

    remainder = path
    if route.strip_prefix and path.startswith(route.path_prefix):
        remainder = path[len(route.path_prefix) :]
    if not remainder.startswith("/"):
        remainder = "/" + remainder

    url = route.upstream.rstrip("/") + remainder
    if query:
        url = f"{url}?{query}"
    return ProxyTarget(url=url, timeout=route.timeout_seconds)


def filter_request_headers(headers: httpx.Headers | dict, upstream_host: str) -> dict[str, str]:
    """Strip hop-by-hop headers and rewrite Host for the upstream.

    Client supplied identity headers are stripped too. An upstream that trusts
    X-Gateway-User-Id must be able to assume the gateway set it, so a client
    sending its own copy has to be discarded here rather than forwarded. This
    is the difference between a header the upstream can trust and one anybody
    can forge.
    """
    cleaned = {
        key: value
        for key, value in dict(headers).items()
        if key.lower() not in HOP_BY_HOP_HEADERS
        and key.lower() != "host"
        and key.lower() not in IDENTITY_HEADERS
    }
    cleaned["host"] = upstream_host
    return cleaned


def identity_headers(user: dict | None) -> dict[str, str]:
    """Build the identity headers for an authenticated caller.

    Returns nothing for an unauthenticated request, so an open route forwards
    no identity at all rather than an empty one.
    """
    if not user:
        return {}

    return {
        USER_ID_HEADER: str(user.get("id", "")),
        USER_EMAIL_HEADER: str(user.get("email", "")),
        # Comma separated, since a header cannot carry a list.
        USER_ROLES_HEADER: ",".join(user.get("roles") or []),
    }


def filter_response_headers(headers: httpx.Headers) -> dict[str, str]:
    """Drop hop-by-hop headers plus the length, which streaming recomputes."""
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() != "content-length"
    }


class ReverseProxy:
    """Owns the shared httpx client.

    One client for the whole process, not one per request. httpx pools
    connections per client, so building a client per request throws the pool
    away every time and pays a fresh TCP and TLS handshake on each hop.
    """

    def __init__(
        self,
        max_connections: int = 100,
        default_timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._max_connections = max_connections
        self._default_timeout = default_timeout
        # Overriding the transport is how the tests point the proxy at the
        # mock upstreams without binding a real port.
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    async def startup(self) -> None:
        limits = httpx.Limits(
            max_connections=self._max_connections,
            max_keepalive_connections=self._max_connections // 2 or 1,
        )
        self._client = httpx.AsyncClient(
            limits=limits,
            timeout=self._default_timeout,
            transport=self._transport,
            # The gateway decides what a redirect means, not httpx. Forwarding
            # the 302 to the client keeps the Location header meaningful.
            follow_redirects=False,
        )
        logger.info("Proxy client ready (max_connections=%d)", self._max_connections)

    async def shutdown(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            logger.info("Proxy client closed")

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("ReverseProxy.startup() was never awaited")
        return self._client

    async def forward(self, request: Request, route: RouteConfig) -> StreamingResponse:
        target = build_target(route, request.url.path, request.url.query)
        upstream_host = httpx.URL(target.url).netloc.decode("ascii")

        headers = filter_request_headers(request.headers, upstream_host)
        # Let the upstream log against the same request ID we are using.
        headers[REQUEST_ID_HEADER] = get_request_id()
        # The auth middleware puts the caller on the scope when a route is
        # protected. Passing it on saves the upstream a second JWT decode.
        headers.update(identity_headers(request.scope.get("user")))

        body = await request.body()

        upstream_request = self.client.build_request(
            method=request.method,
            url=target.url,
            headers=headers,
            content=body,
            timeout=target.timeout,
        )

        started = time.perf_counter()
        try:
            upstream_response = await self.client.send(upstream_request, stream=True)
        except httpx.TimeoutException as exc:
            # A timeout is still time the upstream cost us, so it is recorded
            # rather than left out. Dropping failures would make the upstream
            # look faster the worse it got.
            observe_upstream_duration(route.name, request.method, time.perf_counter() - started)
            logger.warning("Upstream timeout after %ss: %s", target.timeout, target.url)
            raise UpstreamTimeout(route.name, f"upstream timed out: {target.url}") from exc
        except httpx.RequestError as exc:
            observe_upstream_duration(route.name, request.method, time.perf_counter() - started)
            logger.warning("Upstream unreachable: %s (%s)", target.url, exc)
            raise UpstreamUnavailable(route.name, f"upstream unreachable: {target.url}") from exc

        # Measured to response headers, not to the last byte of the body. The
        # body streams to the client afterwards at whatever rate the client
        # reads, and that is not the upstream being slow.
        observe_upstream_duration(route.name, request.method, time.perf_counter() - started)

        return StreamingResponse(
            upstream_response.aiter_raw(),
            status_code=upstream_response.status_code,
            headers=filter_response_headers(upstream_response.headers),
            # Releasing the connection is deferred until the body is fully
            # sent, otherwise the stream is closed out from under the client.
            background=BackgroundTask(upstream_response.aclose),
        )
