"""Request correlation IDs.

Every request gets an X-Request-ID. If the client sent one we keep it, so a
trace started upstream of the gateway survives the hop. Otherwise we mint one.

The ID is also stashed in a ContextVar, which is what lets a log line written
deep inside the proxy carry the request it belongs to without every function
having to pass the ID down as an argument.
"""

import logging
import uuid
from contextvars import ContextVar

from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "x-request-id"

# Default matters: log records emitted outside a request (startup, shutdown)
# still need something to format.
_request_id: ContextVar[str] = ContextVar("request_id", default="-")

logger = logging.getLogger(__name__)


def get_request_id() -> str:
    return _request_id.get()


def _clean(value: str | None) -> str | None:
    """Accept a client supplied ID only if it looks sane.

    An unbounded header from a client ends up in log files and in metric
    labels, so it gets length capped and stripped of anything that is not
    plainly printable.
    """
    if not value:
        return None
    value = value.strip()
    if not value or len(value) > 128:
        return None
    if not all(c.isalnum() or c in "-_." for c in value):
        return None
    return value


class CorrelationIdMiddleware:
    """Pure ASGI middleware.

    Written against the raw ASGI interface rather than BaseHTTPMiddleware
    because BaseHTTPMiddleware runs the endpoint in a separate task, and a
    ContextVar set in the middleware would not be visible from there.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        incoming = headers.get(REQUEST_ID_HEADER.encode())
        request_id = _clean(incoming.decode("latin-1") if incoming else None) or uuid.uuid4().hex

        token = _request_id.set(request_id)
        # Downstream handlers read it off the scope; the proxy forwards it on.
        scope["request_id"] = request_id

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                raw_headers = list(message.get("headers") or [])
                raw_headers = [
                    (key, value)
                    for key, value in raw_headers
                    if key.lower() != REQUEST_ID_HEADER.encode()
                ]
                raw_headers.append((REQUEST_ID_HEADER.encode(), request_id.encode()))
                message = {**message, "headers": raw_headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            _request_id.reset(token)


class RequestIdLogFilter(logging.Filter):
    """Makes %(request_id)s available to the log formatter."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id()
        return True
