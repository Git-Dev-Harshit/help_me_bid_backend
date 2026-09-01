"""IPO identity and its current canonical data, plus the change-history table.

Design note - identity vs. data
-------------------------------
``ipos`` holds one row per *IPO*, keyed by ``(source, source_ipo_id)``.  The
upstream source exposes a stable numeric id per issue, so repeated scrapes
update the same row rather than inserting duplicates.  Values that move during
the IPO's life (GMP, subscription) are updated in place, and - when they change
- also appended to ``ipo_snapshots`` so history is queryable without bloating
the hot table.

Fields that are filtered, sorted or joined on get real typed columns and
indexes.  Anything else the source happens to publish is preserved verbatim in
the ``raw_data`` JSONB column, so a new upstream field is retained from day one
and can be promoted to a real column later without data loss.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

# Money-ish values: enough headroom for issue sizes in crore, two decimals.
_AMOUNT = Numeric(16, 2)
_PERCENT = Numeric(9, 2)


class IPO(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Canonical, de-duplicated record for a single IPO."""

    __tablename__ = "ipos"

    # --- Identity ---------------------------------------------------------
    source: Mapped[str] = mapped_column(
        String(40), nullable=False, default="investorgain", server_default="investorgain"
    )
    source_ipo_id: Mapped[str] = mapped_column(String(64), nullable=False)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(40), nullable=True)
    slug: Mapped[str | None] = mapped_column(String(255), nullable=True)
    detail_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Classification ---------------------------------------------------
    ipo_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="UNKNOWN", server_default="UNKNOWN"
    )
    exchange: Mapped[str] = mapped_column(
        String(20), nullable=False, default="UNKNOWN", server_default="UNKNOWN"
    )
    # Raw upstream status code (U/O/C/LP/LN...) kept for debugging. The status
    # the API exposes is always derived from the dates below.
    source_status: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # --- Key dates --------------------------------------------------------
    open_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    close_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    allotment_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    listing_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)

    # --- Pricing / size ---------------------------------------------------
    price_min: Mapped[float | None] = mapped_column(_AMOUNT, nullable=True)
    price_max: Mapped[float | None] = mapped_column(_AMOUNT, nullable=True)
    lot_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    issue_size_crore: Mapped[float | None] = mapped_column(_AMOUNT, nullable=True)

    # --- Grey market ------------------------------------------------------
    gmp: Mapped[float | None] = mapped_column(_AMOUNT, nullable=True)
    gmp_percentage: Mapped[float | None] = mapped_column(_PERCENT, nullable=True)
    gmp_low: Mapped[float | None] = mapped_column(_AMOUNT, nullable=True)
    gmp_high: Mapped[float | None] = mapped_column(_AMOUNT, nullable=True)
    estimated_listing_price: Mapped[float | None] = mapped_column(_AMOUNT, nullable=True)

    # --- Demand / quality signals ----------------------------------------
    subscription_times: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pe_ratio: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    has_anchor_investors: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # --- Provenance -------------------------------------------------------
    # Everything the source published that has no canonical column yet.
    raw_data: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    source_updated_text: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_seen_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    last_scraped_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Bumped only when a tracked value actually changed - lets clients poll
    # cheaply for "what moved" without diffing every field.
    data_changed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    snapshots: Mapped[list[IPOSnapshot]] = relationship(
        back_populates="ipo", cascade="all, delete-orphan", lazy="noload"
    )

    __table_args__ = (
        UniqueConstraint("source", "source_ipo_id", name="uq_ipos_source_source_ipo_id"),
        CheckConstraint(
            "price_min IS NULL OR price_max IS NULL OR price_min <= price_max",
            name="price_band_ordered",
        ),
        CheckConstraint(
            "open_date IS NULL OR close_date IS NULL OR open_date <= close_date",
            name="open_before_close",
        ),
        CheckConstraint("lot_size IS NULL OR lot_size > 0", name="lot_size_positive"),
        # --- Query-driven indexes ----------------------------------------
        # The notification worker's hot path: "IPOs closing today with a GMP%
        # at or above a threshold". One composite index serves it entirely.
        Index("ix_ipos_close_date_gmp_percentage", "close_date", "gmp_percentage"),
        Index("ix_ipos_open_date", "open_date"),
        Index("ix_ipos_listing_date", "listing_date"),
        # Listing default sort, and the common "SME issues open now" filter.
        Index("ix_ipos_ipo_type_close_date", "ipo_type", "close_date"),
        Index("ix_ipos_exchange", "exchange"),
        Index("ix_ipos_gmp_percentage", "gmp_percentage"),
        # Substring search over name/symbol, backed by pg_trgm.
        Index(
            "ix_ipos_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<IPO id={self.id} name={self.name!r} close={self.close_date}>"


class IPOSnapshot(TimestampMixin, Base):
    """Point-in-time copy of an IPO's volatile values.

    A row is written only when one of the tracked fields changes, so the table
    grows with market activity rather than with scrape frequency.  ``BIGINT``
    identity keys keep it compact - this is the highest-volume table here.
    """

    __tablename__ = "ipo_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ipo_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("ipos.id", ondelete="CASCADE"), nullable=False
    )
    captured_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    gmp: Mapped[float | None] = mapped_column(_AMOUNT, nullable=True)
    gmp_percentage: Mapped[float | None] = mapped_column(_PERCENT, nullable=True)
    subscription_times: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    price_min: Mapped[float | None] = mapped_column(_AMOUNT, nullable=True)
    price_max: Mapped[float | None] = mapped_column(_AMOUNT, nullable=True)
    lot_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    issue_size_crore: Mapped[float | None] = mapped_column(_AMOUNT, nullable=True)
    open_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    close_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    listing_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    source_status: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Field name -> {"old": ..., "new": ...} for exactly what moved.
    changed_fields: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    scrape_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("scrape_runs.id", ondelete="SET NULL"),
        nullable=True,
    )

    ipo: Mapped[IPO] = relationship(back_populates="snapshots")

    __table_args__ = (
        Index("ix_ipo_snapshots_ipo_id_captured_at", "ipo_id", "captured_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<IPOSnapshot ipo_id={self.ipo_id} at={self.captured_at}>"
