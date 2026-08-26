"""Rate limit middleware tests against a fully wired gateway.

The fixture route table carries /api/limited (fixed window, 3 per 60s),
/api/limited-bucket (token bucket, same budget) and /api/limited-auth, which
is the same but requires a token.
"""


from gateway.middleware.rate_limiter import client_identifier

CREDENTIALS = {"email": "muneeb@example.com", "password": "password123"}


def login(gateway, email=CREDENTIALS["email"]):
    creds = {**CREDENTIALS, "email": email}
    gateway.post("/auth/register", json=creds)
    tokens = gateway.post("/auth/login", json=creds).json()
    return {"Authorization": f"Bearer {tokens['access_token']}"}


class TestEnforcement:
    def test_requests_under_the_limit_pass(self, gateway):
        for _ in range(3):
            assert gateway.get("/api/limited").status_code == 200

    def test_request_over_the_limit_is_rejected(self, gateway):
        for _ in range(3):
            gateway.get("/api/limited")

        response = gateway.get("/api/limited")
        assert response.status_code == 429
        assert response.json()["error"] == "rate_limit_exceeded"

    def test_rejection_carries_the_request_id(self, gateway):
        for _ in range(3):
            gateway.get("/api/limited")

        response = gateway.get("/api/limited", headers={"X-Request-ID": "trace-42"})
        assert response.json()["request_id"] == "trace-42"

    def test_rejected_request_never_reaches_the_upstream(self, gateway):
        """The point of limiting at the gateway: the upstream is protected,
        not just the client informed."""
        for _ in range(3):
            gateway.get("/api/limited")

        response = gateway.get("/api/limited")
        assert response.status_code == 429
        assert "x-upstream-service" not in response.headers

    def test_unlimited_routes_are_untouched(self, gateway):
        for _ in range(10):
            assert gateway.get("/api/orders").status_code == 200

    def test_token_bucket_route_is_enforced_too(self, gateway):
        for _ in range(3):
            assert gateway.get("/api/limited-bucket").status_code == 200
        assert gateway.get("/api/limited-bucket").status_code == 429


class TestHeaders:
    def test_allowed_responses_report_the_limit(self, gateway):
        response = gateway.get("/api/limited")
        assert response.headers["x-ratelimit-limit"] == "3"
        assert response.headers["x-ratelimit-remaining"] == "2"
        assert int(response.headers["x-ratelimit-reset"]) > 0

    def test_remaining_counts_down(self, gateway):
        seen = [gateway.get("/api/limited").headers["x-ratelimit-remaining"] for _ in range(3)]
        assert seen == ["2", "1", "0"]

    def test_rejected_responses_carry_headers_too(self, gateway):
        for _ in range(3):
            gateway.get("/api/limited")

        response = gateway.get("/api/limited")
        assert response.headers["x-ratelimit-limit"] == "3"
        assert response.headers["x-ratelimit-remaining"] == "0"

    def test_rejection_says_when_to_retry(self, gateway):
        for _ in range(3):
            gateway.get("/api/limited")

        response = gateway.get("/api/limited")
        assert int(response.headers["retry-after"]) > 0

    def test_allowed_responses_have_no_retry_after(self, gateway):
        assert "retry-after" not in gateway.get("/api/limited").headers

    def test_reset_is_rounded_up_not_down(self, gateway):
        """Reporting 0 for a window that has not reset invites an immediate
        retry that fails again."""
        response = gateway.get("/api/limited-bucket")
        assert int(response.headers["x-ratelimit-reset"]) >= 1


class TestScoping:
    def test_routes_have_separate_budgets(self, gateway):
        for _ in range(3):
            gateway.get("/api/limited")
        assert gateway.get("/api/limited").status_code == 429
        # A different route with its own budget is unaffected.
        assert gateway.get("/api/limited-bucket").status_code == 200

    def test_different_users_have_separate_budgets(self, gateway):
        first = login(gateway, "one@example.com")
        second = login(gateway, "two@example.com")

        for _ in range(3):
            gateway.get("/api/limited-auth", headers=first)
        assert gateway.get("/api/limited-auth", headers=first).status_code == 429

        # The second user has not spent anything.
        assert gateway.get("/api/limited-auth", headers=second).status_code == 200

    def test_authenticated_requests_are_charged_to_the_user(self, gateway):
        """Charging by IP when a user is known would put everyone behind one
        NAT into a shared bucket."""
        headers = login(gateway)
        for _ in range(3):
            gateway.get("/api/limited-auth", headers=headers)
        assert gateway.get("/api/limited-auth", headers=headers).status_code == 429

        # Same address, different user, so the budget is not shared.
        other = login(gateway, "other@example.com")
        assert gateway.get("/api/limited-auth", headers=other).status_code == 200


class TestClientIdentifier:
    def test_authenticated_user_is_identified_by_id(self):
        request = _fake_request(user={"id": "u-1", "email": "a@example.com", "roles": []})
        assert client_identifier(request) == "user:u-1"

    def test_anonymous_falls_back_to_the_address(self):
        request = _fake_request(client_host="10.0.0.5")
        assert client_identifier(request) == "ip:10.0.0.5"

    def test_forwarded_header_wins_over_the_socket_address(self):
        request = _fake_request(
            client_host="10.0.0.5", headers={"x-forwarded-for": "203.0.113.9, 10.0.0.1"}
        )
        assert client_identifier(request) == "ip:203.0.113.9"

    def test_user_identity_wins_over_the_address(self):
        request = _fake_request(
            user={"id": "u-1", "email": "a@example.com", "roles": []},
            client_host="10.0.0.5",
        )
        assert client_identifier(request) == "user:u-1"


class TestUnknownPaths:
    def test_unknown_path_is_a_404_not_a_429(self, gateway):
        """The rate limiter must not answer for routes it does not know, or an
        unknown path could be limited into a 429 instead of a 404."""
        for _ in range(10):
            response = gateway.get("/no/such/route")
        assert response.status_code == 404


def _fake_request(user=None, client_host=None, headers=None):
    from starlette.requests import Request

    raw_headers = [(k.encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/limited",
        "headers": raw_headers,
        "query_string": b"",
        "client": (client_host, 12345) if client_host else None,
    }
    if user is not None:
        scope["user"] = user
    return Request(scope)
