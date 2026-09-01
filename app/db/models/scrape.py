"""Scraper observability: per-run records and retained raw payloads.

The raw payload lives in its own table rather than as a column on
``scrape_runs``.  Payloads are large (~140 KB of HTML, ~90 KB of JSON) and are
only ever read when debugging a specific failure, so keeping them out of the
run table means the "how have recent scrapes gone?" query never drags blobs
through the buffer cache.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ScrapeRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One execution of the scrape pipeline, successful or not."""

    __tablename__ = "scrape_runs"

    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    finished_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    status: Mapped[str] = mapped_column(String(20), nullable=False)
    strategy: Mapped[str] = mapped_column(
        String(20), nullable=False, default="NONE", server_default="NONE"
    )
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- Outcome counters -------------------------------------------------
    records_found: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    records_valid: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    records_invalid: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    ipos_inserted: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    ipos_updated: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    ipos_unchanged: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    snapshots_created: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # 0.0-1.0; below SCRAPER_MIN_CONFIDENCE the run refuses to persist.
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)

    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Non-fatal observations: unmapped columns, rows dropped in validation, ...
    warnings: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    # Column-name -> canonical-field mapping actually used, so a structure
    # change is visible by diffing consecutive runs.
    field_mapping: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    raw_payloads: Mapped[list[ScrapeRawPayload]] = relationship(
        back_populates="scrape_run", cascade="all, delete-orphan", lazy="noload"
    )

    __table_args__ = (
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="confidence_range",
        ),
        Index("ix_scrape_runs_started_at_status", "started_at", "status"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ScrapeRun id={self.id} status={self.status} found={self.records_found}>"


class ScrapeRawPayload(UUIDPrimaryKeyMixin, Base):
    """A retained upstream response, kept so failures can be reproduced.

    Retention is governed by ``SCRAPER_RAW_RETENTION_MODE`` (always /
    on_failure / never) and ``SCRAPER_RAW_RETENTION_DAYS``; a scheduled job
    prunes anything older.  ``content_hash`` lets consecutive identical
    payloads be recognised without comparing the full body.
    """

    __tablename__ = "scrape_raw_payloads"

    scrape_run_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("scrape_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    captured_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_type: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    scrape_run: Mapped[ScrapeRun] = relationship(back_populates="raw_payloads")

    __table_args__ = (
        Index("ix_scrape_raw_payloads_captured_at", "captured_at"),
        Index("ix_scrape_raw_payloads_scrape_run_id", "scrape_run_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ScrapeRawPayload run={self.scrape_run_id} bytes={self.byte_size}>"
