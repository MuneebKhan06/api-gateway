"""Token creation and validation.

Two token types, deliberately different in lifetime and in what they can do:

- access: short lived (15 minutes), sent on every request, validated by
  signature alone so the hot path needs no database lookup.
- refresh: long lived (7 days), sent only when an access token expires, and
  recorded in the database so it can actually be revoked.

The `typ` claim separates them. Without it, a refresh token would be a valid
access token, which would hand a 7 day credential to every request.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt

logger = logging.getLogger(__name__)

ACCESS_TOKEN_TYPE = "access"
REFRESH_TOKEN_TYPE = "refresh"


class TokenError(Exception):
    """Base for every rejection reason, so callers can catch one type."""


class TokenExpired(TokenError):
    pass


class TokenInvalid(TokenError):
    pass


class WrongTokenType(TokenError):
    pass


@dataclass(frozen=True)
class TokenClaims:
    subject: str
    email: str
    roles: list[str]
    token_type: str
    jti: str
    issued_at: datetime
    expires_at: datetime

    @property
    def is_access(self) -> bool:
        return self.token_type == ACCESS_TOKEN_TYPE


@dataclass(frozen=True)
class IssuedToken:
    token: str
    jti: str
    expires_at: datetime


class JWTHandler:
    def __init__(
        self,
        secret_key: str,
        algorithm: str = "HS256",
        access_ttl_seconds: int = 900,
        refresh_ttl_seconds: int = 604800,
    ) -> None:
        if not secret_key:
            raise ValueError("a JWT secret key is required")
        self._secret = secret_key
        self._algorithm = algorithm
        self._access_ttl = access_ttl_seconds
        self._refresh_ttl = refresh_ttl_seconds

    def create_access_token(self, user_id: str, email: str, roles: list[str]) -> IssuedToken:
        """Roles are embedded in the token so authorization needs no lookup.

        The cost is staleness: a role change does not take effect until the
        token expires, so at most 15 minutes.
        """
        return self._create(user_id, email, roles, ACCESS_TOKEN_TYPE, self._access_ttl)

    def create_refresh_token(self, user_id: str, email: str, roles: list[str]) -> IssuedToken:
        return self._create(user_id, email, roles, REFRESH_TOKEN_TYPE, self._refresh_ttl)

    def _create(
        self, user_id: str, email: str, roles: list[str], token_type: str, ttl: int
    ) -> IssuedToken:
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=ttl)
        jti = uuid.uuid4().hex

        payload = {
            "sub": user_id,
            "email": email,
            "roles": roles,
            "typ": token_type,
            "jti": jti,
            "iat": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
        }
        token = jwt.encode(payload, self._secret, algorithm=self._algorithm)
        return IssuedToken(token=token, jti=jti, expires_at=expires_at)

    def decode(self, token: str, expected_type: str | None = None) -> TokenClaims:
        """Verify signature and expiry, then optionally enforce the token type."""
        try:
            payload = jwt.decode(
                token,
                self._secret,
                algorithms=[self._algorithm],
                options={"require": ["exp", "iat", "sub", "jti"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise TokenExpired("token has expired") from exc
        except jwt.InvalidTokenError as exc:
            # Covers a bad signature, a malformed token and missing claims.
            raise TokenInvalid(str(exc)) from exc

        token_type = payload.get("typ")
        if expected_type is not None and token_type != expected_type:
            raise WrongTokenType(f"expected a {expected_type} token, got {token_type}")

        return TokenClaims(
            subject=payload["sub"],
            email=payload.get("email", ""),
            roles=list(payload.get("roles") or []),
            token_type=token_type or "",
            jti=payload["jti"],
            issued_at=datetime.fromtimestamp(payload["iat"], tz=timezone.utc),
            expires_at=datetime.fromtimestamp(payload["exp"], tz=timezone.utc),
        )

    def decode_access(self, token: str) -> TokenClaims:
        return self.decode(token, expected_type=ACCESS_TOKEN_TYPE)

    def decode_refresh(self, token: str) -> TokenClaims:
        return self.decode(token, expected_type=REFRESH_TOKEN_TYPE)


def extract_bearer_token(header_value: str | None) -> str | None:
    """Pull the token out of an Authorization header.

    Returns None rather than raising, because a missing or malformed header
    and a bad token are the same outcome for the caller: not authenticated.
    """
    if not header_value:
        return None
    parts = header_value.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None
