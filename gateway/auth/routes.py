"""Auth endpoints.

Mounted on the gateway itself rather than proxied anywhere, which is why
routes.yaml declares /auth with a null upstream.
"""

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from gateway.auth.blacklist import TokenBlacklist
from gateway.auth.jwt_handler import JWTHandler, extract_bearer_token
from gateway.auth.service import (
    AccountDisabled,
    AuthService,
    EmailAlreadyRegistered,
    InvalidCredentials,
    InvalidRefreshToken,
)
from gateway.middleware.correlation import get_request_id
from gateway.schemas.auth import (
    LoginRequest,
    MessageResponse,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)
from gateway.schemas.gateway import GatewayError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


def client_ip(request: Request) -> str | None:
    """Best effort client address.

    X-Forwarded-For is trusted here because the gateway is expected to sit
    behind a load balancer that sets it. Exposed directly to the internet this
    header is client controlled and should not be believed.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


async def get_auth_service(request: Request) -> AuthService:
    """One service per request, bound to one session.

    The session is committed by the caller's context manager, so a handler
    that raises leaves nothing half written.
    """
    state = request.app.state
    async with state.database.session() as session:
        yield AuthService(
            session=session,
            jwt_handler=state.jwt_handler,
            blacklist=TokenBlacklist(state.redis.client, cache=state.blacklist_cache),
            access_ttl_seconds=state.settings.access_token_ttl_seconds,
        )


def error(status_code: int, code: str, detail: str) -> JSONResponse:
    body = GatewayError(error=code, detail=detail, request_id=get_request_id())
    return JSONResponse(status_code=status_code, content=body.model_dump())


@router.post("/register", response_model=UserResponse, status_code=201)
async def register(
    payload: RegisterRequest,
    request: Request,
    service: AuthService = Depends(get_auth_service),
):
    try:
        user = await service.register(
            payload.email, payload.password, request_id=get_request_id(), ip=client_ip(request)
        )
    except EmailAlreadyRegistered as exc:
        return error(409, "email_taken", str(exc))

    return UserResponse(id=user.id, email=user.email, roles=user.role_list)


@router.post("/login", response_model=TokenResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    service: AuthService = Depends(get_auth_service),
):
    try:
        pair = await service.login(
            payload.email, payload.password, request_id=get_request_id(), ip=client_ip(request)
        )
    except InvalidCredentials as exc:
        # Deliberately the same response whether the email is unknown or the
        # password is wrong, so this cannot be used to enumerate accounts.
        return error(401, "invalid_credentials", str(exc))
    except AccountDisabled as exc:
        return error(403, "account_disabled", str(exc))

    return TokenResponse(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        expires_in=pair.expires_in,
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(payload: RefreshRequest, service: AuthService = Depends(get_auth_service)):
    try:
        pair = await service.refresh(payload.refresh_token, request_id=get_request_id())
    except InvalidRefreshToken as exc:
        return error(401, "invalid_refresh_token", str(exc))

    return TokenResponse(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        expires_in=pair.expires_in,
    )


@router.post("/logout", response_model=MessageResponse)
async def logout(
    request: Request,
    payload: RefreshRequest | None = None,
    service: AuthService = Depends(get_auth_service),
):
    token = extract_bearer_token(request.headers.get("authorization"))
    if token is None:
        return error(401, "missing_token", "an access token is required to log out")

    try:
        await service.logout(
            token,
            refresh_token=payload.refresh_token if payload else None,
            request_id=get_request_id(),
        )
    except InvalidCredentials as exc:
        return error(401, "invalid_token", str(exc))

    return MessageResponse(message="logged out")


def build_jwt_handler(settings) -> JWTHandler:
    return JWTHandler(
        secret_key=settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
        access_ttl_seconds=settings.access_token_ttl_seconds,
        refresh_ttl_seconds=settings.refresh_token_ttl_seconds,
    )
