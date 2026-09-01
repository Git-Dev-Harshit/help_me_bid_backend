"""User account model."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.db.models.device import Device
    from app.db.models.notification import NotificationDelivery, NotificationPreference


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An authenticated account.

    ``phone_number`` is the login identity and is always stored normalised to
    E.164 (see :func:`app.core.security.normalize_phone_number`), which is what
    makes the unique constraint meaningful across input formats.  ``email`` is
    optional and lower-cased by the auth service before it reaches the database.
    """

    __tablename__ = "users"

    phone_number: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)

    name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    last_login_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    preferences: Mapped[list[NotificationPreference]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )
    devices: Mapped[list[Device]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )
    deliveries: Mapped[list[NotificationDelivery]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="noload"
    )

    __table_args__ = (
        CheckConstraint(r"phone_number ~ '^\+[1-9][0-9]{6,17}$'", name="phone_number_e164"),
        # Unique only across rows that actually carry an email.
        Index(
            "uq_users_email",
            "email",
            unique=True,
            postgresql_where=text("email IS NOT NULL"),
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<User id={self.id} phone={self.phone_number}>"
