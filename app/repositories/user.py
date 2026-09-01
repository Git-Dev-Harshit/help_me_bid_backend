"""User data access."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.user import User
from app.utils.dates import utc_now


class UserRepository:
    """Reads and writes for the ``users`` table."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, user_id: uuid.UUID) -> User | None:
        return await self.session.get(User, user_id)

    async def get_by_phone(self, phone_number: str) -> User | None:
        """Look up by normalised E.164 number - the login path."""
        statement = select(User).where(User.phone_number == phone_number)
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def get_by_email(self, email: str) -> User | None:
        statement = select(User).where(User.email == email.lower())
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def exists_by_phone(self, phone_number: str) -> bool:
        statement = select(User.id).where(User.phone_number == phone_number).limit(1)
        return (await self.session.execute(statement)).first() is not None

    async def create(
        self,
        *,
        phone_number: str,
        hashed_password: str,
        name: str | None = None,
        email: str | None = None,
    ) -> User:
        user = User(
            phone_number=phone_number,
            hashed_password=hashed_password,
            name=name,
            email=email.lower() if email else None,
        )
        self.session.add(user)
        await self.session.flush()
        return user

    async def touch_last_login(self, user: User) -> None:
        user.last_login_at = utc_now()
        await self.session.flush()

    async def update_profile(
        self, user: User, *, name: str | None = None, email: str | None = None
    ) -> User:
        if name is not None:
            user.name = name
        if email is not None:
            user.email = email.lower()
        await self.session.flush()
        return user
