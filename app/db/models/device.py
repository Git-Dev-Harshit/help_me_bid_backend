"""Registered push targets (Flutter / web clients)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.db.models.user import User


class Device(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A push-notification target belonging to a user.

    ``push_token`` is globally unique: FCM reissues a token to whichever app
    install currently holds it, so on re-registration the token is *moved* to
    the new owner rather than duplicated (see the device repository).
    """

    __tablename__ = "devices"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    device_type: Mapped[str] = mapped_column(String(20), nullable=False)
    push_token: Mapped[str] = mapped_column(Text, nullable=False, unique=True)

    device_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    app_version: Mapped[str | None] = mapped_column(String(40), nullable=True)

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    last_seen_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Set when a provider reports the token as permanently invalid, so the
    # worker stops retrying it.
    invalidated_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped[User] = relationship(back_populates="devices")

    __table_args__ = (
        # The delivery worker only ever asks for a user's *active* devices.
        Index(
            "ix_devices_user_id_active",
            "user_id",
            postgresql_where=text("is_active"),
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Device id={self.id} user={self.user_id} type={self.device_type}>"
