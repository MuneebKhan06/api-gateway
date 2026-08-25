"""Password hashing.

Uses the bcrypt library directly rather than passlib. passlib 1.7.4 is the
current release and it breaks against bcrypt 4.1 and newer: it probes for a
version attribute that no longer exists, then fails on a length check it
should have handled itself. passlib has not had a release since 2020, so
pinning bcrypt backwards to keep it working means carrying an old crypto
dependency to satisfy a dead abstraction. Calling bcrypt directly is a few
more lines and one less thing to go wrong.

The cost factor is deliberately slow. A fast password hash is a liability,
because it makes an offline attack on a stolen database cheap.
"""

import bcrypt

# bcrypt hashes at most 72 bytes and silently ignores the rest, so two long
# passwords sharing a 72 byte prefix would be interchangeable. The register
# schema rejects anything longer instead of letting that happen quietly.
MAX_PASSWORD_BYTES = 72

# Work factor. 12 is the common default: a few hundred milliseconds per hash,
# slow enough to hurt an attacker, fast enough for a login endpoint.
BCRYPT_ROUNDS = 12


class PasswordTooLong(ValueError):
    pass


def _encode(password: str) -> bytes:
    encoded = password.encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise PasswordTooLong(f"password exceeds {MAX_PASSWORD_BYTES} bytes when encoded")
    return encoded


def hash_password(password: str) -> str:
    salt = bcrypt.gensalt(rounds=BCRYPT_ROUNDS)
    return bcrypt.hashpw(_encode(password), salt).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Constant time comparison, False on anything malformed.

    A stored hash that does not parse is a data problem, not an authentication
    success, so it returns False rather than raising into the request handler.
    """
    try:
        return bcrypt.checkpw(_encode(password), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False
