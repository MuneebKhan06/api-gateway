import time
from datetime import timezone

import jwt
import pytest

from gateway.auth.jwt_handler import (
    ACCESS_TOKEN_TYPE,
    REFRESH_TOKEN_TYPE,
    JWTHandler,
    TokenExpired,
    TokenInvalid,
    WrongTokenType,
    extract_bearer_token,
)

SECRET = "test-secret-key"


@pytest.fixture
def handler():
    return JWTHandler(secret_key=SECRET, access_ttl_seconds=900, refresh_ttl_seconds=604800)


class TestCreation:
    def test_access_token_round_trips(self, handler):
        issued = handler.create_access_token("user-1", "a@example.com", ["user"])
        claims = handler.decode_access(issued.token)

        assert claims.subject == "user-1"
        assert claims.email == "a@example.com"
        assert claims.roles == ["user"]
        assert claims.is_access

    def test_refresh_token_round_trips(self, handler):
        issued = handler.create_refresh_token("user-1", "a@example.com", ["user"])
        claims = handler.decode_refresh(issued.token)
        assert claims.token_type == REFRESH_TOKEN_TYPE
        assert not claims.is_access

    def test_each_token_gets_a_unique_jti(self, handler):
        first = handler.create_access_token("user-1", "a@example.com", ["user"])
        second = handler.create_access_token("user-1", "a@example.com", ["user"])
        assert first.jti != second.jti

    def test_reported_expiry_matches_the_claim(self, handler):
        issued = handler.create_access_token("user-1", "a@example.com", ["user"])
        claims = handler.decode_access(issued.token)
        assert int(issued.expires_at.timestamp()) == int(claims.expires_at.timestamp())

    def test_refresh_lives_longer_than_access(self, handler):
        access = handler.create_access_token("u", "a@example.com", [])
        refresh = handler.create_refresh_token("u", "a@example.com", [])
        assert refresh.expires_at > access.expires_at

    def test_empty_secret_is_rejected(self):
        with pytest.raises(ValueError):
            JWTHandler(secret_key="")


class TestValidation:
    def test_expired_token_is_rejected(self):
        handler = JWTHandler(secret_key=SECRET, access_ttl_seconds=1)
        issued = handler.create_access_token("user-1", "a@example.com", ["user"])
        time.sleep(1.1)
        with pytest.raises(TokenExpired):
            handler.decode_access(issued.token)

    def test_tampered_signature_is_rejected(self, handler):
        issued = handler.create_access_token("user-1", "a@example.com", ["user"])
        tampered = issued.token[:-4] + "aaaa"
        with pytest.raises(TokenInvalid):
            handler.decode_access(tampered)

    def test_token_signed_with_another_secret_is_rejected(self, handler):
        other = JWTHandler(secret_key="a-different-secret")
        issued = other.create_access_token("user-1", "a@example.com", ["user"])
        with pytest.raises(TokenInvalid):
            handler.decode_access(issued.token)

    def test_garbage_is_rejected(self, handler):
        with pytest.raises(TokenInvalid):
            handler.decode_access("not-a-jwt")

    def test_refresh_token_is_not_accepted_as_access(self, handler):
        """The point of the typ claim: a 7 day credential must not work as a
        15 minute one."""
        issued = handler.create_refresh_token("user-1", "a@example.com", ["user"])
        with pytest.raises(WrongTokenType):
            handler.decode_access(issued.token)

    def test_access_token_is_not_accepted_as_refresh(self, handler):
        issued = handler.create_access_token("user-1", "a@example.com", ["user"])
        with pytest.raises(WrongTokenType):
            handler.decode_refresh(issued.token)

    def test_alg_none_token_is_rejected(self, handler):
        """The classic JWT attack: strip the signature and claim alg=none."""
        forged = jwt.encode(
            {"sub": "attacker", "jti": "x", "iat": 0, "exp": 9999999999, "typ": "access"},
            key="",
            algorithm="none",
        )
        with pytest.raises(TokenInvalid):
            handler.decode_access(forged)

    def test_token_missing_required_claims_is_rejected(self, handler):
        incomplete = jwt.encode({"sub": "user-1"}, SECRET, algorithm="HS256")
        with pytest.raises(TokenInvalid):
            handler.decode_access(incomplete)

    def test_decode_without_expected_type_accepts_either(self, handler):
        access = handler.create_access_token("u", "a@example.com", [])
        refresh = handler.create_refresh_token("u", "a@example.com", [])
        assert handler.decode(access.token).token_type == ACCESS_TOKEN_TYPE
        assert handler.decode(refresh.token).token_type == REFRESH_TOKEN_TYPE

    def test_claim_timestamps_are_timezone_aware(self, handler):
        issued = handler.create_access_token("u", "a@example.com", [])
        claims = handler.decode_access(issued.token)
        assert claims.issued_at.tzinfo is timezone.utc
        assert claims.expires_at.tzinfo is timezone.utc


class TestBearerExtraction:
    def test_standard_header(self):
        assert extract_bearer_token("Bearer abc.def.ghi") == "abc.def.ghi"

    def test_scheme_is_case_insensitive(self):
        assert extract_bearer_token("bearer abc") == "abc"

    @pytest.mark.parametrize(
        "header", [None, "", "abc", "Basic abc", "Bearer", "Bearer   ", "Token abc"]
    )
    def test_unusable_headers_return_none(self, header):
        assert extract_bearer_token(header) is None

    def test_extra_whitespace_is_trimmed(self):
        assert extract_bearer_token("Bearer    abc  ") == "abc"
