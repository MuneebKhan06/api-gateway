"""Application settings.

Everything the gateway needs to boot lives here. Values are read from the
environment (or a .env file in development) so nothing has to be hardcoded
per deployment.
"""

from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: The placeholder shipped in .env.example. Fine for development, fatal in
#: production: anyone who has read the repository can forge tokens with it.
DEFAULT_JWT_SECRET = "change-me-in-production"

#: Below this, a brute force against the signing key is worth attempting.
MIN_PRODUCTION_SECRET_LENGTH = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Gateway process
    app_name: str = "api-gateway"
    environment: Literal["development", "test", "production"] = "development"
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    # Where the route table lives. Relative paths resolve from the repo root.
    routes_file: str = "routes.yaml"

    # Redis holds rate limit counters, the token blacklist and breaker state
    redis_url: str = "redis://localhost:6379/0"
    redis_max_connections: int = 50

    # Postgres holds users, refresh tokens and the audit log
    database_url: str = "postgresql+asyncpg://gateway:gateway@localhost:5432/gateway"

    # JWT
    jwt_secret_key: str = DEFAULT_JWT_SECRET
    jwt_algorithm: str = "HS256"
    access_token_ttl_seconds: int = 900  # 15 minutes
    refresh_token_ttl_seconds: int = 604800  # 7 days
    # How long an instance may serve a cached "not revoked" answer before
    # rechecking Redis. Raising this cuts Redis load and widens the window in
    # which a revoked token is still accepted.
    blacklist_cache_ttl_seconds: float = 5.0

    # Rate limiting defaults. A route may override these in routes.yaml.
    default_rate_limit_algorithm: Literal[
        "token_bucket", "sliding_window", "fixed_window"
    ] = "token_bucket"
    default_rate_limit_requests: int = 100
    default_rate_limit_window_seconds: int = 60

    # Circuit breaker defaults, applied per upstream service
    breaker_failure_threshold: int = 5
    breaker_failure_window_seconds: int = 60
    breaker_recovery_timeout_seconds: int = 30
    breaker_success_threshold: int = 2

    # Reverse proxy
    upstream_timeout_seconds: float = 30.0
    upstream_max_connections: int = 100

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @model_validator(mode="after")
    def reject_insecure_production_config(self) -> "Settings":
        """Refuse to start in production with development defaults.

        These are the settings that are harmless locally and dangerous in
        production, and the failure mode for each is silent: nothing looks
        wrong until someone forges a token. Failing at startup makes a
        misconfigured deployment impossible to miss, which is much better than
        discovering it from an incident.

        Only enforced when ENVIRONMENT is production, so development and tests
        are untouched.
        """
        if not self.is_production:
            return self

        problems: list[str] = []

        if self.jwt_secret_key == DEFAULT_JWT_SECRET:
            problems.append(
                "JWT_SECRET_KEY is still the example value, so anyone who has "
                "read this repository can forge a valid token"
            )
        elif len(self.jwt_secret_key) < MIN_PRODUCTION_SECRET_LENGTH:
            problems.append(
                f"JWT_SECRET_KEY is shorter than {MIN_PRODUCTION_SECRET_LENGTH} "
                "characters, which is short enough to be worth brute forcing"
            )

        if self.debug:
            problems.append("DEBUG is on, which leaks internals in error responses")

        # A wildcard bind is normal inside a container and wrong on a host
        # that is directly reachable, so this is a warning-shaped problem
        # rather than a hard error. Left out deliberately.

        if "@gateway:gateway@" in self.database_url or ":gateway@" in self.database_url:
            problems.append("DATABASE_URL still uses the example database password")

        if problems:
            raise ValueError(
                "Refusing to start in production with insecure configuration:\n  - "
                + "\n  - ".join(problems)
            )

        return self


@lru_cache
def get_settings() -> Settings:
    """Cached so the env is parsed once per process, not once per request."""
    return Settings()
