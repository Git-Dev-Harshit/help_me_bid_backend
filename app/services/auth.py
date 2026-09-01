"""Authentication and account services.

Kept deliberately separate from the route layer so that refresh tokens, OTP
login or phone verification can be added here without touching the HTTP
surface.
"""

from __future__ import annotations

import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    InactiveUserError,
    InvalidCredentialsError,
    PhoneAlreadyRegisteredError,
    UserNotFoundError,
)
from app.core.logging import get_logger
from app.core.security import (
    create_access_token,
    hash_password,
    normalize_phone_number,
    password_needs_rehash,
    validate_password_strength,
    verify_password,
)
from app.db.models.user import User
from app.repositories.user import UserRepository
from app.schemas.auth import LoginRequest, RegisterRequest, TokenResponse

logger = get_logger(__name__)


class AuthService:
    """Registration, login and token issuing."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.users = UserRepository(session)

    async def register(self, payload: RegisterRequest) -> User:
        """Create an account.

        The phone number is normalised to E.164 first, so the uniqueness check
        is meaningful regardless of how the client formatted it.
        """
        phone = normalize_phone_number(payload.phone_number)
        validate_password_strength(payload.password)

        if await self.users.exists_by_phone(phone):
            # The phone number is the login identity, so a conflict here is
            # unavoidable information; it is not an enumeration leak the way a
            # "this email exists" message on a password reset would be.
            raise PhoneAlreadyRegisteredError()

        try:
            user = await self.users.create(
                phone_number=phone,
                hashed_password=hash_password(payload.password),
                name=payload.name.strip() if payload.name else None,
                email=str(payload.email) if payload.email else None,
            )
            await self.session.commit()
        except IntegrityError as exc:
            # Two concurrent registrations for the same number: the database
            # constraint is the real arbiter, not the check above.
            await self.session.rollback()
            raise PhoneAlreadyRegisteredError() from exc

        logger.info("auth.user_registered", extra={"user_id": str(user.id)})
        return user

    async def authenticate(self, payload: LoginRequest) -> User:
        """Verify credentials, returning the user on success."""
        try:
            phone = normalize_phone_number(payload.phone_number)
        except Exception:
            # An unparseable number cannot match any account. Report it exactly
            # as a wrong password so the endpoint reveals nothing.
            logger.info("auth.login_failed", extra={"reason": "unparseable_phone"})
            raise InvalidCredentialsError() from None

        user = await self.users.get_by_phone(phone)
        if user is None:
            # Hash a dummy password anyway so the response time does not
            # distinguish "no such account" from "wrong password".
            verify_password(payload.password, _DUMMY_HASH)
            logger.info("auth.login_failed", extra={"reason": "unknown_account"})
            raise InvalidCredentialsError()

        if not verify_password(payload.password, user.hashed_password):
            logger.info(
                "auth.login_failed",
                extra={"reason": "bad_password", "user_id": str(user.id)},
            )
            raise InvalidCredentialsError()

        if not user.is_active:
            logger.info("auth.login_blocked", extra={"user_id": str(user.id)})
            raise InactiveUserError()

        # Transparently upgrade hashes when Argon2 parameters change.
        if password_needs_rehash(user.hashed_password):
            user.hashed_password = hash_password(payload.password)

        await self.users.touch_last_login(user)
        await self.session.commit()
        logger.info("auth.login_succeeded", extra={"user_id": str(user.id)})
        return user

    @staticmethod
    def issue_token(user: User) -> TokenResponse:
        """Mint an access token for an authenticated user."""
        token, expires_in = create_access_token(user.id)
        return TokenResponse(access_token=token, token_type="bearer", expires_in=expires_in)

    async def get_active_user(self, user_id: uuid.UUID) -> User:
        """Load a user for an authenticated request, rejecting disabled accounts."""
        user = await self.users.get_by_id(user_id)
        if user is None:
            raise UserNotFoundError()
        if not user.is_active:
            raise InactiveUserError()
        return user


#: A real Argon2 hash of a value nobody can supply, used to equalise timing on
#: the "unknown account" path.
_DUMMY_HASH = hash_password("timing-equalisation-placeholder")
