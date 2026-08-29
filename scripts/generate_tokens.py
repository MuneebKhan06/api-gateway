#!/usr/bin/env python
"""Generate JWTs for poking at the gateway by hand.

Registering and logging in through the API is the honest way to get a token,
but it needs the database up. This signs one directly with the configured
secret, which is enough to exercise the auth middleware while testing.

    python scripts/generate_tokens.py
    python scripts/generate_tokens.py --roles user,admin --ttl 3600
    python scripts/generate_tokens.py --curl

The tokens are real: same secret, same claims, same validation path. What they
are not is backed by a user row, so anything that looks the user up (refresh,
logout) will not find one.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway.auth.jwt_handler import JWTHandler  # noqa: E402
from gateway.config import get_settings  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate signed access and refresh tokens for testing"
    )
    parser.add_argument(
        "--user-id",
        default="00000000-0000-0000-0000-000000000001",
        help="value for the sub claim",
    )
    parser.add_argument("--email", default="test@example.com")
    parser.add_argument(
        "--roles",
        default="user",
        help="comma separated, embedded in the token so authorization needs no lookup",
    )
    parser.add_argument(
        "--ttl",
        type=int,
        default=None,
        help="access token lifetime in seconds (default: whatever the config says)",
    )
    parser.add_argument(
        "--expired",
        action="store_true",
        help="issue an already expired access token, for testing the 401 path",
    )
    parser.add_argument("--json", action="store_true", help="print as JSON")
    parser.add_argument(
        "--curl", action="store_true", help="print a ready to paste curl command"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = get_settings()

    if settings.jwt_secret_key == "change-me-in-production":
        print(
            "Warning: signing with the default development secret. Tokens will "
            "only be accepted by a gateway using the same default.\n",
            file=sys.stderr,
        )

    # A negative TTL produces a token whose exp is already in the past, which
    # is the simplest way to exercise the expiry branch without waiting.
    access_ttl = -60 if args.expired else (args.ttl or settings.access_token_ttl_seconds)

    handler = JWTHandler(
        secret_key=settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
        access_ttl_seconds=access_ttl,
        refresh_ttl_seconds=settings.refresh_token_ttl_seconds,
    )

    roles = [role.strip() for role in args.roles.split(",") if role.strip()]
    access = handler.create_access_token(args.user_id, args.email, roles)
    refresh = handler.create_refresh_token(args.user_id, args.email, roles)

    if args.json:
        print(
            json.dumps(
                {
                    "access_token": access.token,
                    "refresh_token": refresh.token,
                    "expires_at": access.expires_at.isoformat(),
                    "roles": roles,
                },
                indent=2,
            )
        )
        return 0

    if args.curl:
        print(
            f"curl -H 'Authorization: Bearer {access.token}' "
            f"http://localhost:{settings.port}/api/orders"
        )
        return 0

    print(f"user:    {args.user_id}")
    print(f"email:   {args.email}")
    print(f"roles:   {', '.join(roles)}")
    print(f"expires: {access.expires_at.isoformat()}")
    print()
    print("access token:")
    print(access.token)
    print()
    print("refresh token:")
    print(refresh.token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
