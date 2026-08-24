"""Schemas for the route table and the gateway's own introspection endpoints."""

from typing import Literal

from pydantic import BaseModel, Field, model_validator

Algorithm = Literal["token_bucket", "sliding_window", "fixed_window"]


class RateLimitConfig(BaseModel):
    algorithm: Algorithm = "token_bucket"
    requests: int = Field(gt=0)
    window_seconds: int = Field(gt=0)


class RouteConfig(BaseModel):
    """One entry in routes.yaml.

    A route with a null upstream is handled by the gateway itself (health,
    metrics, the auth endpoints) rather than being proxied anywhere.
    """

    path_prefix: str
    upstream: str | None = None
    strip_prefix: bool = True
    timeout_seconds: float = Field(default=30.0, gt=0)
    rate_limit: RateLimitConfig | None = None
    auth_required: bool = True
    circuit_breaker: bool = True

    @model_validator(mode="after")
    def check_prefix_and_upstream(self) -> "RouteConfig":
        if not self.path_prefix.startswith("/"):
            raise ValueError(f"path_prefix must start with a slash: {self.path_prefix!r}")
        if self.path_prefix != "/" and self.path_prefix.endswith("/"):
            raise ValueError(f"path_prefix must not end with a slash: {self.path_prefix!r}")
        if self.upstream is None:
            # Nothing to proxy to, so the proxy-side features do not apply.
            self.circuit_breaker = False
        return self

    @property
    def name(self) -> str:
        """Short label used for metric labels and breaker keys."""
        if self.upstream is None:
            return "gateway"
        host = self.upstream.split("://", 1)[-1]
        return host.split(":", 1)[0].split("/", 1)[0]


class RouteTableConfig(BaseModel):
    routes: list[RouteConfig]


class RouteStatus(BaseModel):
    """What GET /gateway/routes returns for each route."""

    path_prefix: str
    upstream: str | None
    auth_required: bool
    circuit_breaker_state: str
    rate_limit_algorithm: str | None


class GatewayError(BaseModel):
    """Error envelope for failures the gateway itself produces.

    Upstream error bodies are passed through untouched. This shape only
    applies when the gateway is the one refusing or failing the request, so a
    client can tell "the gateway said no" from "the service said no".
    """

    error: str
    detail: str
    request_id: str | None = None
