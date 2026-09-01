"""Password hashing, JWT and phone-normalisation tests."""

from __future__ import annotations

import datetime as dt
import uuid

import jwt
import pytest

from app.core.config import settings
from app.core.exceptions import InvalidTokenError, ValidationError
from app.core.security import (
    create_access_token,
    decode_access_token,
    extract_user_id,
    hash_password,
    normalize_phone_number,
    validate_password_strength,
    verify_password,
)


class TestPasswordHashing:
    def test_hash_is_verifiable_and_not_the_plaintext(self):
        hashed = hash_password("Str0ngPass1")
        assert hashed != "Str0ngPass1"
        assert hashed.startswith("$argon2")
        assert verify_password("Str0ngPass1", hashed)

    def test_wrong_password_fails(self):
        assert not verify_password("wrong", hash_password("Str0ngPass1"))

    def test_hashes_are_salted(self):
        assert hash_password("same") != hash_password("same")

    def test_malformed_hash_returns_false_rather_than_raising(self):
        assert not verify_password("anything", "not-a-hash")


class TestPasswordPolicy:
    @pytest.mark.parametrize("password", ["Str0ngPass1", "abcdefg1", "a1" * 40])
    def test_accepts_valid_passwords(self, password):
        validate_password_strength(password)

    @pytest.mark.parametrize(
        ("password", "reason"),
        [
            ("short1", "too short"),
            ("alllettersonly", "no digit"),
            ("12345678", "no letter"),
            ("x1" * 100, "too long"),
        ],
    )
    def test_rejects_weak_passwords(self, password, reason):
        with pytest.raises(ValidationError):
            validate_password_strength(password)


class TestPhoneNormalisation:
    @pytest.mark.parametrize(
        "raw",
        ["9876543210", "+919876543210", "+91 98765 43210", "098765 43210", "+91-98765-43210"],
    )
    def test_equivalent_inputs_collapse_to_one_identity(self, raw):
        """This is what makes the uniqueness constraint meaningful."""
        assert normalize_phone_number(raw, region="IN") == "+919876543210"

    @pytest.mark.parametrize("raw", ["", "   ", "abc", "12", "+9999999999999999999"])
    def test_rejects_invalid_numbers(self, raw):
        with pytest.raises(ValidationError):
            normalize_phone_number(raw, region="IN")

    def test_international_numbers_are_kept(self):
        assert normalize_phone_number("+14155552671", region="IN") == "+14155552671"


class TestJWT:
    def test_round_trip(self):
        user_id = uuid.uuid4()
        token, expires_in = create_access_token(user_id)
        assert expires_in == settings.jwt_access_token_expire_minutes * 60
        assert extract_user_id(token) == user_id

    def test_claims_are_present(self):
        payload = decode_access_token(create_access_token(uuid.uuid4())[0])
        assert payload["type"] == "access"
        assert payload["iss"] == settings.jwt_issuer
        assert "jti" in payload and "exp" in payload

    def test_expired_token_is_rejected(self):
        token, _ = create_access_token(uuid.uuid4(), expires_minutes=1)
        expired = jwt.encode(
            {
                **decode_access_token(token),
                "exp": int(
                    (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5)).timestamp()
                ),
            },
            settings.jwt_secret_key,
            algorithm=settings.jwt_algorithm,
        )
        with pytest.raises(InvalidTokenError):
            decode_access_token(expired)

    def test_token_signed_with_another_key_is_rejected(self):
        forged = jwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "type": "access",
                "iss": settings.jwt_issuer,
                "exp": int((dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)).timestamp()),
            },
            "an-attackers-key",
            algorithm="HS256",
        )
        with pytest.raises(InvalidTokenError):
            decode_access_token(forged)

    def test_garbage_is_rejected(self):
        with pytest.raises(InvalidTokenError):
            decode_access_token("not.a.token")
