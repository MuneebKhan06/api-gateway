"""The gateway tells the upstream who is calling.

An upstream behind the gateway should not have to decode the JWT again. It
gets the caller's id, email and roles as headers, and it must be able to trust
them, which means a client cannot set them itself.
"""

import pytest

from gateway.proxy import (
    USER_EMAIL_HEADER,
    USER_ID_HEADER,
    USER_ROLES_HEADER,
    identity_headers,
)

CREDENTIALS = {"email": "muneeb@example.com", "password": "password123"}


@pytest.fixture
def auth_header(gateway):
    gateway.post("/auth/register", json=CREDENTIALS)
    tokens = gateway.post("/auth/login", json=CREDENTIALS).json()
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def upstream_headers(gateway, path="/api/protected/_echo/headers", **kwargs):
    return gateway.get(path, **kwargs).json()["headers"]


class TestIdentityHeaderBuilder:
    def test_builds_all_three(self):
        built = identity_headers({"id": "u-1", "email": "a@example.com", "roles": ["user", "admin"]})
        assert built[USER_ID_HEADER] == "u-1"
        assert built[USER_EMAIL_HEADER] == "a@example.com"
        assert built[USER_ROLES_HEADER] == "user,admin"

    def test_no_identity_for_an_unauthenticated_request(self):
        assert identity_headers(None) == {}
        assert identity_headers({}) == {}

    def test_empty_roles_becomes_an_empty_string(self):
        built = identity_headers({"id": "u-1", "email": "a@example.com", "roles": []})
        assert built[USER_ROLES_HEADER] == ""


class TestForwarding:
    def test_authenticated_request_carries_identity_upstream(self, gateway, auth_header):
        headers = upstream_headers(gateway, headers=auth_header)
        assert headers[USER_ID_HEADER]
        assert headers[USER_EMAIL_HEADER] == CREDENTIALS["email"]
        assert headers[USER_ROLES_HEADER] == "user"

    def test_forwarded_id_matches_the_registered_user(self, gateway):
        created = gateway.post("/auth/register", json=CREDENTIALS).json()
        tokens = gateway.post("/auth/login", json=CREDENTIALS).json()

        headers = upstream_headers(
            gateway, headers={"Authorization": f"Bearer {tokens['access_token']}"}
        )
        assert headers[USER_ID_HEADER] == created["id"]

    def test_open_route_forwards_no_identity(self, gateway):
        # /api/orders is open, so there is no caller to name.
        headers = upstream_headers(gateway, path="/api/orders/_echo/headers")
        assert USER_ID_HEADER not in headers

    def test_request_id_still_reaches_the_upstream(self, gateway, auth_header):
        headers = upstream_headers(
            gateway, headers={**auth_header, "X-Request-ID": "trace-77"}
        )
        assert headers["x-request-id"] == "trace-77"


class TestSpoofing:
    def test_client_supplied_identity_is_stripped(self, gateway, auth_header):
        """The important one. If a client could set this header itself, the
        upstream's trust in it would be worthless."""
        headers = upstream_headers(
            gateway,
            headers={**auth_header, USER_ID_HEADER: "administrator"},
        )
        assert headers[USER_ID_HEADER] != "administrator"

    def test_spoofed_roles_are_replaced_with_the_real_ones(self, gateway, auth_header):
        headers = upstream_headers(
            gateway,
            headers={**auth_header, USER_ROLES_HEADER: "admin,superuser"},
        )
        assert headers[USER_ROLES_HEADER] == "user"

    def test_spoofing_on_an_open_route_is_dropped_entirely(self, gateway):
        headers = upstream_headers(
            gateway,
            path="/api/orders/_echo/headers",
            headers={USER_ID_HEADER: "administrator"},
        )
        assert USER_ID_HEADER not in headers
