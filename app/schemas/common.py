"""Response envelopes shared across the API.

Successful responses return the resource (or a paginated envelope) directly.
Failures always return :class:`ErrorResponse`, so a client can branch on the
presence of ``error`` and switch on the stable ``error.code``.
"""

from __future__ import annotations

import math
from typing import Annotated, Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20

PageNumber = Annotated[int, Field(ge=1, description="1-based page number.")]
PageSize = Annotated[
    int,
    Field(ge=1, le=MAX_PAGE_SIZE, description=f"Items per page (max {MAX_PAGE_SIZE})."),
]


class PaginationMeta(BaseModel):
    """Cursor-free pagination metadata."""

    page: int = Field(description="Current 1-based page number.", examples=[1])
    page_size: int = Field(description="Items requested per page.", examples=[20])
    total_items: int = Field(description="Total rows matching the filters.", examples=[125])
    total_pages: int = Field(description="Total pages available.", examples=[7])
    has_next: bool = Field(description="True when a following page exists.")
    has_previous: bool = Field(description="True when a preceding page exists.")

    @classmethod
    def build(cls, *, page: int, page_size: int, total_items: int) -> PaginationMeta:
        total_pages = math.ceil(total_items / page_size) if total_items else 0
        return cls(
            page=page,
            page_size=page_size,
            total_items=total_items,
            total_pages=total_pages,
            has_next=page < total_pages,
            has_previous=page > 1 and total_items > 0,
        )


class Page(BaseModel, Generic[T]):
    """A page of results plus its metadata."""

    items: list[T] = Field(description="Results for the current page.")
    pagination: PaginationMeta


class ErrorDetail(BaseModel):
    """The machine-readable part of a failure."""

    code: str = Field(
        description="Stable, machine-readable error identifier.",
        examples=["IPO_NOT_FOUND"],
    )
    message: str = Field(
        description="Human-readable explanation. Wording may change; do not parse it.",
        examples=["IPO not found"],
    )
    details: dict[str, Any] | None = Field(
        default=None,
        description="Optional structured context, e.g. per-field validation errors.",
    )


class ErrorResponse(BaseModel):
    """The response body returned for every handled failure."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "success": False,
                "error": {"code": "IPO_NOT_FOUND", "message": "IPO not found"},
            }
        }
    )

    success: Literal[False] = False
    error: ErrorDetail


class HealthResponse(BaseModel):
    """Liveness/readiness payload."""

    status: Literal["ok", "degraded"] = Field(description="Overall service state.")
    version: str = Field(description="Application version.")
    environment: str = Field(description="Configured APP_ENV.")
    database: Literal["ok", "unavailable"] = Field(description="Database connectivity.")


class MessageResponse(BaseModel):
    """Simple acknowledgement for operations with no resource to return."""

    message: str = Field(examples=["Device deregistered."])
