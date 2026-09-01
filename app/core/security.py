"""Password hashing, JWT issuing/verification and phone-number normalisation.

Passwords are hashed with Argon2id (the PHC winner and OWASP's current first
choice).  Tokens are stateless HS256 JWTs carrying the user id in ``sub``.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Final

import jwt
import phonenumbers
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import settings
from app.core.exceptions import InvalidTokenError, ValidationError

# Argon2id with the library defaults, which track the OWASP recommendation.
_password_hasher: Final = PasswordHasher()

TOKEN_TYPE_ACCESS: Final = "access"


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    """Hash a plaintext password with Argon2id."""
    return _password_hasher.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    """Check a plaintext password against a stored hash.

    Returns ``False`` rather than raising for any mismatch or malformed hash,
    so callers can treat every failure identically and avoid leaking which
    part of the credential was wrong.
    """
    try:
        return _password_hasher.verify(hashed, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(hashed: str) -> bool:
    """True when the hash was produced with outdated Argon2 parameters."""
    try:
        return _password_hasher.check_needs_rehash(hashed)
    except InvalidHashError:
        return False


def validate_password_strength(password: str) -> None:
    """Enforce the minimum password policy.

    Deliberately modest: a length floor plus a requirement to mix letters and
    digits.  Long passphrases are not penalised, which is what NIST recommends.
    """
    minimum = settings.password_min_length
    if len(password) < minimum:
        raise ValidationError(
            f"Password must be at least {minimum} characters long.",
            code="WEAK_PASSWORD",
        )
    if len(password) > 128:
        raise ValidationError(
            "Password must be at most 128 characters long.",
            code="WEAK_PASSWORD",
        )
    if not any(char.isalpha() for char in password):
        raise ValidationError(
            "Password must contain at least one letter.",
            code="WEAK_PASSWORD",
        )
    if not any(char.isdigit() for char in password):
        raise ValidationError(
            "Password must contain at least one digit.",
            code="WEAK_PASSWORD",
        )


# ---------------------------------------------------------------------------
# Phone numbers
# ---------------------------------------------------------------------------
def normalize_phone_number(raw: str, region: str | None = None) -> str:
    """Normalise a phone number to E.164 (for example ``+919876543210``).

    Storing one canonical representation is what makes the uniqueness
    constraint meaningful - otherwise ``9876543210``, ``+91 98765 43210`` and
    ``091-98765-43210`` would each create a separate account.
    """
    region = region or settings.default_phone_region
    candidate = (raw or "").strip()
    if not candidate:
        raise ValidationError("Phone number is required.", code="INVALID_PHONE_NUMBER")

    try:
        parsed = phonenumbers.parse(candidate, None if candidate.startswith("+") else region)
    except phonenumbers.NumberParseException as exc:
        raise ValidationError(
            "Phone number could not be parsed.", code="INVALID_PHONE_NUMBER"
        ) from exc

    if not phonenumbers.is_valid_number(parsed):
        raise ValidationError("Phone number is not valid.", code="INVALID_PHONE_NUMBER")

    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------
def create_access_token(
    user_id: uuid.UUID,
    *,
    expires_minutes: int | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> tuple[str, int]:
    """Issue a signed access token.

    Returns ``(token, expires_in_seconds)``.  The ``jti`` claim is included so
    a future revocation list can reference individual tokens.
    """
    expires_minutes = expires_minutes or settings.jwt_access_token_expire_minutes
    now = dt.datetime.now(dt.UTC)
    expires_at = now + dt.timedelta(minutes=expires_minutes)

    payload: dict[str, Any] = {
        "sub": str(user_id),
        "type": TOKEN_TYPE_ACCESS,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "iss": settings.jwt_issuer,
        "jti": uuid.uuid4().hex,
    }
    if extra_claims:
        payload.update(extra_claims)

    token = jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    return token, int((expires_at - now).total_seconds())


def decode_access_token(token: str) -> dict[str, Any]:
    """Validate a token's signature, expiry and issuer, returning its claims."""
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
            issuer=settings.jwt_issuer,
            options={"require": ["exp", "sub", "iss"]},
        )
    except jwt.PyJWTError as exc:
        raise InvalidTokenError() from exc

    if payload.get("type") != TOKEN_TYPE_ACCESS:
        raise InvalidTokenError()
    return payload


def extract_user_id(token: str) -> uuid.UUID:
    """Return the authenticated user id carried by a token."""
    payload = decode_access_token(token)
    try:
        return uuid.UUID(str(payload["sub"]))
    except (KeyError, ValueError) as exc:
        raise InvalidTokenError() from exc
