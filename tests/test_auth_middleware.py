"""Auth middleware tests.

The fixture route table has /api/protected marked auth_required and
/api/orders left open, so both paths are covered against one gateway.
"""

import time

import pytest

from gateway.auth.blacklist import TokenBlacklist

CREDENTIALS = {"email": "muneeb@example.com", "password": "password123"}


@pytest.fixture
def tokens(gateway):
    gateway.post("/auth/register", json=CREDENTIALS)
    return gateway.post("/auth/login", json=CREDENTIALS).json()


@pytest.fixture
def auth_header(tokens):
    return {"Authorization": f"Bearer {tokens['access_token']}"}


class TestOpenRoutes:
    def test_open_route_needs_no_token(self, gateway):
        assert gateway.get("/api/orders").status_code == 200

    def test_health_needs_no_token(self, gateway):
        assert gateway.get("/health").status_code == 200

    def test_login_needs_no_token(self, gateway):
        gateway.post("/auth/register", json=CREDENTIALS)
        assert gateway.post("/auth/login", json=CREDENTIALS).status_code == 200


class TestProtectedRoutes:
    def test_valid_token_is_let_through(self, gateway, auth_header):
        response = gateway.get("/api/protected", headers=auth_header)
        assert response.status_code == 200
        assert response.json()["service"] == "service-a"

    def test_missing_token_is_rejected(self, gateway):
        response = gateway.get("/api/protected")
        assert response.status_code == 401
        assert response.json()["error"] == "missing_token"

    def test_401_carries_the_www_authenticate_header(self, gateway):
        response = gateway.get("/api/protected")
        assert response.headers["www-authenticate"] == "Bearer"

    def test_garbage_token_is_rejected(self, gateway):
        response = gateway.get("/api/protected", headers={"Authorization": "Bearer nonsense"})
        assert response.status_code == 401
        assert response.json()["error"] == "invalid_token"

    def test_malformed_header_is_rejected(self, gateway, tokens):
        # Missing the Bearer scheme entirely.
        response = gateway.get(
            "/api/protected", headers={"Authorization": tokens["access_token"]}
        )
        assert response.status_code == 401
        assert response.json()["error"] == "missing_token"

    def test_refresh_token_cannot_authenticate_a_request(self, gateway, tokens):
        response = gateway.get(
            "/api/protected",
            headers={"Authorization": f"Bearer {tokens['refresh_token']}"},
        )
        assert response.status_code == 401
        assert response.json()["error"] == "wrong_token_type"

    def test_expired_token_is_rejected(self, gateway, settings):
        """Mint a token that expires almost immediately and wait it out."""
        from gateway.auth.jwt_handler import JWTHandler

        short_lived = JWTHandler(secret_key=settings.jwt_secret_key, access_ttl_seconds=1)
        issued = short_lived.create_access_token("user-1", "a@example.com", ["user"])
        time.sleep(1.1)

        response = gateway.get(
            "/api/protected", headers={"Authorization": f"Bearer {issued.token}"}
        )
        assert response.status_code == 401
        assert response.json()["error"] == "token_expired"

    def test_token_signed_with_another_secret_is_rejected(self, gateway):
        from gateway.auth.jwt_handler import JWTHandler

        forged = JWTHandler(secret_key="not-the-gateway-secret").create_access_token(
            "attacker", "evil@example.com", ["admin"]
        )
        response = gateway.get(
            "/api/protected", headers={"Authorization": f"Bearer {forged.token}"}
        )
        assert response.status_code == 401


class TestBlacklistEnforcement:
    def test_logout_stops_the_access_token_working(self, gateway, tokens, auth_header):
        """This is the whole reason the blacklist exists: without it, logout
        is cosmetic and the token keeps working until it expires."""
        assert gateway.get("/api/protected", headers=auth_header).status_code == 200

        gateway.post("/auth/logout", headers=auth_header, json={"refresh_token": tokens["refresh_token"]})

        response = gateway.get("/api/protected", headers=auth_header)
        assert response.status_code == 401
        assert response.json()["error"] == "token_revoked"

    def test_revoking_one_token_does_not_affect_another(self, gateway):
        gateway.post("/auth/register", json=CREDENTIALS)
        first = gateway.post("/auth/login", json=CREDENTIALS).json()
        second = gateway.post("/auth/login", json=CREDENTIALS).json()

        gateway.post(
            "/auth/logout",
            headers={"Authorization": f"Bearer {first['access_token']}"},
            json={"refresh_token": first["refresh_token"]},
        )

        # The second session belongs to the same user and must still work.
        response = gateway.get(
            "/api/protected", headers={"Authorization": f"Bearer {second['access_token']}"}
        )
        assert response.status_code == 200


class TestUnknownPaths:
    def test_unknown_path_is_a_404_not_a_401(self, gateway):
        """The auth layer must not answer for routes it does not know about,
        otherwise every unknown path reports itself as protected."""
        response = gateway.get("/no/such/route")
        assert response.status_code == 404
        assert response.json()["error"] == "route_not_found"
