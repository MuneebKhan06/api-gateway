import dataclasses

import pytest

from gateway.rate_limit.base import KEY_PREFIX, RateLimitResult, client_key


class TestClientKey:
    def test_key_is_namespaced(self):
        key = client_key("user-1", "/api/orders", "token_bucket")
        assert key.startswith(f"{KEY_PREFIX}:token_bucket:/api/orders:")

    def test_same_input_gives_the_same_key(self):
        assert client_key("user-1", "/api/orders", "token_bucket") == client_key(
            "user-1", "/api/orders", "token_bucket"
        )

    def test_different_clients_get_different_keys(self):
        assert client_key("user-1", "/api/orders", "token_bucket") != client_key(
            "user-2", "/api/orders", "token_bucket"
        )

    def test_routes_have_separate_budgets(self):
        """A client's allowance on one route must not be spent by another."""
        assert client_key("user-1", "/api/orders", "token_bucket") != client_key(
            "user-1", "/api/users", "token_bucket"
        )

    def test_algorithms_do_not_share_counters(self):
        """Switching a route's algorithm must not inherit counters written in
        a format the new algorithm cannot read."""
        assert client_key("user-1", "/api/orders", "token_bucket") != client_key(
            "user-1", "/api/orders", "fixed_window"
        )

    def test_identifier_is_not_stored_in_plain_text(self):
        # An email out of a JWT should not end up readable in Redis keys.
        key = client_key("muneeb@example.com", "/api/orders", "token_bucket")
        assert "muneeb@example.com" not in key
        assert "muneeb" not in key


class TestRateLimitResult:
    def test_rejected_is_the_inverse_of_allowed(self):
        allowed = RateLimitResult(allowed=True, limit=10, remaining=9, reset_after=60)
        denied = RateLimitResult(allowed=False, limit=10, remaining=0, reset_after=60)
        assert allowed.rejected is False
        assert denied.rejected is True

    def test_result_is_immutable(self):
        result = RateLimitResult(allowed=True, limit=10, remaining=9, reset_after=60)
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.allowed = False
