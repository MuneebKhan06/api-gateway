"""Production configuration guards.

Every setting checked here is harmless in development and dangerous in
production, and each fails silently: nothing looks wrong until someone forges
a token. Failing at startup makes a misconfigured deployment impossible to
miss.
"""

import pytest

from gateway.config import DEFAULT_JWT_SECRET, MIN_PRODUCTION_SECRET_LENGTH, Settings

STRONG_SECRET = "s" * MIN_PRODUCTION_SECRET_LENGTH
SAFE_DATABASE_URL = "postgresql+asyncpg://gwuser:a-real-password@db:5432/gateway"


def production(**overrides) -> Settings:
    base = {
        "environment": "production",
        "jwt_secret_key": STRONG_SECRET,
        "database_url": SAFE_DATABASE_URL,
    }
    return Settings(**{**base, **overrides})


class TestDevelopmentIsUnaffected:
    def test_defaults_are_fine_in_development(self):
        settings = Settings(environment="development")
        assert settings.jwt_secret_key == DEFAULT_JWT_SECRET

    def test_defaults_are_fine_in_test(self):
        assert Settings(environment="test").jwt_secret_key == DEFAULT_JWT_SECRET

    def test_debug_is_allowed_in_development(self):
        assert Settings(environment="development", debug=True).debug is True


class TestProductionSecrets:
    def test_a_well_configured_production_setup_starts(self):
        assert production().is_production is True

    def test_the_example_secret_is_refused(self):
        """It is in the repository, so it is not a secret."""
        with pytest.raises(ValueError, match="forge a valid token"):
            production(jwt_secret_key=DEFAULT_JWT_SECRET)

    def test_a_short_secret_is_refused(self):
        with pytest.raises(ValueError, match="brute forcing"):
            production(jwt_secret_key="short")

    def test_a_secret_at_the_minimum_length_is_accepted(self):
        assert production(jwt_secret_key="k" * MIN_PRODUCTION_SECRET_LENGTH)

    def test_the_example_database_password_is_refused(self):
        with pytest.raises(ValueError, match="example database password"):
            production(
                database_url="postgresql+asyncpg://gateway:gateway@postgres:5432/gateway"
            )


class TestProductionDebug:
    def test_debug_is_refused_in_production(self):
        with pytest.raises(ValueError, match="DEBUG is on"):
            production(debug=True)


class TestErrorReporting:
    def test_every_problem_is_reported_at_once(self):
        """Fixing one misconfiguration only to be told about the next is a
        slow way to learn what is wrong."""
        with pytest.raises(ValueError) as exc:
            Settings(
                environment="production",
                jwt_secret_key=DEFAULT_JWT_SECRET,
                debug=True,
                database_url="postgresql+asyncpg://gateway:gateway@postgres:5432/gateway",
            )

        message = str(exc.value)
        assert "forge a valid token" in message
        assert "DEBUG is on" in message
        assert "example database password" in message
