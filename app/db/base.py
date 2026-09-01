"""Declarative base and shared column mixins.

Importing this module pulls in every ORM model, which is what gives Alembic's
``--autogenerate`` a complete view of the schema.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, ClassVar

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Explicit naming conventions so Alembic emits stable, predictable constraint
# names instead of database-generated ones (which makes migrations reversible).
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base class for every ORM model."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    # Fetch server-generated values (created_at/updated_at, gen_random_uuid)
    # in the same statement via RETURNING. Without this, reading updated_at
    # after a commit triggers a lazy refresh, which raises MissingGreenlet
    # under async sessions.
    __mapper_args__: ClassVar[dict[str, Any]] = {"eager_defaults": True}


class UUIDPrimaryKeyMixin:
    """A surrogate UUID primary key.

    UUIDs keep ids non-enumerable in public APIs and let a client generate an
    id before insert, which sequential integers cannot.
    """

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )


class TimestampMixin:
    """``created_at`` / ``updated_at``, both stored as UTC.

    Defaults are set server-side so rows written outside the ORM (migrations,
    manual SQL) are stamped correctly too.
    """

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
