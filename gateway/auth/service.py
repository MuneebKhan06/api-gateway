"""Authentication flows.

The endpoint layer handles HTTP; this handles what registering, logging in,
refreshing and logging out actually mean. Keeping them apart means the rules
can be tested without going through a request.
"""

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from gateway.auth.blacklist import TokenBlacklist
from gateway.auth.jwt_handler import JWTHandler, TokenError
from gateway.auth.passwords import hash_password, verify_password
from gateway.db.models import User
from gateway.db.repository import AuditRepository, RefreshTokenRepository, UserRepository

logger = logging.getLogger(__name__)


class AuthError(Exception):
    """Something about the request was not acceptable."""


class EmailAlreadyRegistered(AuthError):
    pass


class InvalidCredentials(AuthError):
    pass


class AccountDisabled(AuthError):
    pass


class InvalidRefreshToken(AuthError):
    pass


class TokenPair:
    def __init__(self, access_token: str, refresh_token: str, expires_in: int) -> None:
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.expires_in = expires_in


class AuthService:
    def __init__(
        self,
        session: AsyncSession,
        jwt_handler: JWTHandler,
        blacklist: TokenBlacklist,
        access_ttl_seconds: int,
    ) -> None:
        self._session = session
        self._jwt = jwt_handler
        self._blacklist = blacklist
        self._access_ttl = access_ttl_seconds
        self._users = UserRepository(session)
        self._tokens = RefreshTokenRepository(session)
        self._audit = AuditRepository(session)

    async def register(
        self, email: str, password: str, request_id: str | None = None, ip: str | None = None
    ) -> User:
        if await self._users.email_exists(email):
            # Recorded because repeated hits on existing accounts is a signal
            # worth having, even though the caller gets a generic error.
            await self._audit.record(
                "register_rejected", detail=email, request_id=request_id, ip_address=ip
            )
            raise EmailAlreadyRegistered("that email is already registered")

        user = await self._users.create(email, hash_password(password))
        await self._audit.record(
            "register", user_id=user.id, request_id=request_id, ip_address=ip
        )
        logger.info("Registered user %s", user.id)
        return user

    async def login(
        self, email: str, password: str, request_id: str | None = None, ip: str | None = None
    ) -> TokenPair:
        user = await self._users.get_by_email(email)

        if user is None:
            # Hash a throwaway password anyway so a request for an unknown
            # email takes about as long as one for a known email. Without
            # this, response timing tells an attacker which accounts exist.
            hash_password(password)
            await self._audit.record(
                "login_failed", detail=email, request_id=request_id, ip_address=ip
            )
            raise InvalidCredentials("email or password is incorrect")

        if not verify_password(password, user.password_hash):
            await self._audit.record(
                "login_failed", user_id=user.id, request_id=request_id, ip_address=ip
            )
            raise InvalidCredentials("email or password is incorrect")

        if not user.is_active:
            await self._audit.record(
                "login_disabled", user_id=user.id, request_id=request_id, ip_address=ip
            )
            raise AccountDisabled("this account is disabled")

        pair = await self._issue_pair(user)
        await self._audit.record("login", user_id=user.id, request_id=request_id, ip_address=ip)
        return pair

    async def refresh(self, refresh_token: str, request_id: str | None = None) -> TokenPair:
        """Exchange a refresh token for a new pair.

        Signature validity is not enough. The token must also still be present
        and unrevoked in the database, which is what makes revocation real.
        """
        try:
            claims = self._jwt.decode_refresh(refresh_token)
        except TokenError as exc:
            raise InvalidRefreshToken(str(exc)) from exc

        stored = await self._tokens.get_by_jti(claims.jti)
        if stored is None or not stored.is_usable():
            await self._audit.record(
                "refresh_rejected", user_id=claims.subject, request_id=request_id
            )
            raise InvalidRefreshToken("this refresh token is no longer valid")

        user = await self._users.get_by_id(claims.subject)
        if user is None or not user.is_active:
            raise InvalidRefreshToken("this refresh token is no longer valid")

        # Rotate: the old token is spent, so a stolen copy is worth nothing
        # after the legitimate client uses it once.
        await self._tokens.revoke(claims.jti)
        pair = await self._issue_pair(user)
        await self._audit.record("refresh", user_id=user.id, request_id=request_id)
        return pair

    async def logout(
        self, access_token: str, refresh_token: str | None = None, request_id: str | None = None
    ) -> None:
        """Blacklist the access token and revoke the refresh token.

        Both halves are needed. Blacklisting only the access token leaves the
        client able to mint a new one; revoking only the refresh token leaves
        the current access token working until it expires.
        """
        try:
            claims = self._jwt.decode_access(access_token)
        except TokenError as exc:
            raise InvalidCredentials("that access token is not valid") from exc

        await self._blacklist.add(claims.jti, claims.expires_at, reason="logout")

        if refresh_token:
            try:
                refresh_claims = self._jwt.decode_refresh(refresh_token)
                await self._tokens.revoke(refresh_claims.jti)
            except TokenError:
                # A bad refresh token should not fail a logout: the access
                # token is already blacklisted, which is the important half.
                logger.info("Logout received an unusable refresh token, ignoring it")

        await self._audit.record("logout", user_id=claims.subject, request_id=request_id)

    async def logout_everywhere(self, user_id: str, request_id: str | None = None) -> int:
        revoked = await self._tokens.revoke_all_for_user(user_id)
        await self._audit.record(
            "logout_all", user_id=user_id, detail=f"{revoked} sessions", request_id=request_id
        )
        return revoked

    async def _issue_pair(self, user: User) -> TokenPair:
        roles = user.role_list
        access = self._jwt.create_access_token(user.id, user.email, roles)
        refresh = self._jwt.create_refresh_token(user.id, user.email, roles)
        await self._tokens.store(user.id, refresh.jti, refresh.expires_at)
        return TokenPair(access.token, refresh.token, self._access_ttl)
