"""Auth endpoint tests against a fully wired gateway."""

import pytest

CREDENTIALS = {"email": "muneeb@example.com", "password": "password123"}


def register(client, **overrides):
    return client.post("/auth/register", json={**CREDENTIALS, **overrides})


def login(client, **overrides):
    return client.post("/auth/login", json={**CREDENTIALS, **overrides})


@pytest.fixture
def tokens(gateway):
    register(gateway)
    return login(gateway).json()


class TestRegister:
    def test_creates_a_user(self, gateway):
        response = register(gateway)
        assert response.status_code == 201

        body = response.json()
        assert body["email"] == CREDENTIALS["email"]
        assert body["roles"] == ["user"]
        assert body["id"]

    def test_password_is_never_returned(self, gateway):
        body = register(gateway).json()
        assert "password" not in body
        assert "password_hash" not in body

    def test_duplicate_email_is_rejected(self, gateway):
        register(gateway)
        response = register(gateway)
        assert response.status_code == 409
        assert response.json()["error"] == "email_taken"

    def test_email_casing_does_not_create_a_second_account(self, gateway):
        register(gateway)
        assert register(gateway, email="MUNEEB@example.com").status_code == 409

    @pytest.mark.parametrize("bad_email", ["not-an-email", "", "@example.com", "a@"])
    def test_malformed_email_is_rejected(self, gateway, bad_email):
        assert register(gateway, email=bad_email).status_code == 422

    def test_short_password_is_rejected(self, gateway):
        assert register(gateway, password="short").status_code == 422

    def test_overlong_password_is_rejected(self, gateway):
        # Past 72 bytes bcrypt ignores the rest, so it is refused up front.
        assert register(gateway, password="a" * 100).status_code == 422


class TestLogin:
    def test_returns_a_token_pair(self, gateway):
        register(gateway)
        response = login(gateway)
        assert response.status_code == 200

        body = response.json()
        assert body["access_token"]
        assert body["refresh_token"]
        assert body["token_type"] == "bearer"
        assert body["expires_in"] == 900

    def test_access_and_refresh_tokens_differ(self, tokens):
        assert tokens["access_token"] != tokens["refresh_token"]

    def test_wrong_password_is_rejected(self, gateway):
        register(gateway)
        response = login(gateway, password="wrongpassword")
        assert response.status_code == 401
        assert response.json()["error"] == "invalid_credentials"

    def test_unknown_email_is_rejected(self, gateway):
        response = login(gateway, email="nobody@example.com")
        assert response.status_code == 401

    def test_unknown_email_and_wrong_password_look_identical(self, gateway):
        """Different responses here would let anyone enumerate registered
        accounts by watching for the error message to change."""
        register(gateway)
        unknown = login(gateway, email="nobody@example.com")
        wrong = login(gateway, password="wrongpassword")
        assert unknown.status_code == wrong.status_code
        assert unknown.json()["detail"] == wrong.json()["detail"]


class TestRefresh:
    def test_exchanges_for_a_new_pair(self, gateway, tokens):
        response = gateway.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
        assert response.status_code == 200
        assert response.json()["access_token"] != tokens["access_token"]

    def test_old_refresh_token_stops_working_after_use(self, gateway, tokens):
        """Rotation: a refresh token is spent once used, so a stolen copy is
        worthless after the real client refreshes."""
        first = gateway.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
        assert first.status_code == 200

        replay = gateway.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
        assert replay.status_code == 401
        assert replay.json()["error"] == "invalid_refresh_token"

    def test_access_token_is_not_accepted_as_a_refresh_token(self, gateway, tokens):
        response = gateway.post("/auth/refresh", json={"refresh_token": tokens["access_token"]})
        assert response.status_code == 401

    def test_garbage_is_rejected(self, gateway):
        assert gateway.post("/auth/refresh", json={"refresh_token": "nonsense"}).status_code == 401


class TestLogout:
    def test_logout_succeeds(self, gateway, tokens):
        response = gateway.post(
            "/auth/logout",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
            json={"refresh_token": tokens["refresh_token"]},
        )
        assert response.status_code == 200
        assert response.json()["message"] == "logged out"

    def test_logout_revokes_the_refresh_token(self, gateway, tokens):
        gateway.post(
            "/auth/logout",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
            json={"refresh_token": tokens["refresh_token"]},
        )
        # The refresh token must not be usable to mint a new access token.
        response = gateway.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
        assert response.status_code == 401

    def test_logout_without_a_token_is_rejected(self, gateway):
        response = gateway.post("/auth/logout", json={"refresh_token": "x"})
        assert response.status_code == 401
        assert response.json()["error"] == "missing_token"

    def test_logout_with_a_bad_access_token_is_rejected(self, gateway):
        response = gateway.post("/auth/logout", headers={"Authorization": "Bearer nonsense"})
        assert response.status_code == 401

    def test_logout_tolerates_a_bad_refresh_token(self, gateway, tokens):
        """The access token is already blacklisted by then, which is the half
        that matters, so a broken refresh token should not fail the call."""
        response = gateway.post(
            "/auth/logout",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
            json={"refresh_token": "nonsense"},
        )
        assert response.status_code == 200


class TestAuditTrail:
    def test_errors_carry_the_request_id(self, gateway):
        response = gateway.post(
            "/auth/login",
            json={"email": "nobody@example.com", "password": "password123"},
            headers={"X-Request-ID": "trace-9"},
        )
        assert response.json()["request_id"] == "trace-9"
