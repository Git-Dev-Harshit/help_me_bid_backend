"""Shared pytest fixtures.

Unit tests need no services at all.  Integration tests (marked
``@pytest.mark.integration``) need PostgreSQL; they build the schema from the
ORM metadata into a dedicated test database and tear it down afterwards, so
they never touch development data.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def api_fixture_path() -> pathlib.Path:
    return FIXTURES / "api" / "investorgain_report.json"


@pytest.fixture(scope="session")
def api_payload_text(api_fixture_path: pathlib.Path) -> str:
    """The captured upstream JSON response, verbatim."""
    return api_fixture_path.read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def api_payload(api_payload_text: str) -> dict:
    return json.loads(api_payload_text)


@pytest.fixture(scope="session")
def html_fixture_dir() -> pathlib.Path:
    return FIXTURES / "html"


@pytest.fixture
def load_html(html_fixture_dir: pathlib.Path):
    """Return a loader for a named HTML fixture."""

    def _load(name: str) -> str:
        return (html_fixture_dir / name).read_text(encoding="utf-8")

    return _load


@pytest.fixture(scope="session")
def reference_date() -> dt.date:
    """Anchor date matching when the fixtures were captured.

    Pinning this keeps year inference deterministic for dates the source
    prints without a year.
    """
    return dt.date(2026, 9, 1)


# ---------------------------------------------------------------------------
# Integration database
# ---------------------------------------------------------------------------
def _test_database_url() -> str:
    from app.core.config import settings

    # Same server, separate database, so a test run can never touch dev data.
    base, _, _ = settings.database_url.rpartition("/")
    return f"{base}/ipo_tracker_test"


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def test_engine() -> AsyncGenerator[object, None]:
    """Create the test database and its schema; drop it at the end."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.core.config import settings
    from app.db.models import Base

    admin = create_async_engine(
        settings.database_url.rpartition("/")[0] + "/postgres",
        isolation_level="AUTOCOMMIT",
    )
    async with admin.connect() as connection:
        from sqlalchemy import text

        await connection.execute(text("DROP DATABASE IF EXISTS ipo_tracker_test"))
        await connection.execute(text("CREATE DATABASE ipo_tracker_test"))
    await admin.dispose()

    # NullPool: every session opens its own connection on the loop that is
    # actually running, so a pooled asyncpg connection can never be handed
    # to a different event loop than the one that created it.
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(_test_database_url(), poolclass=NullPool)
    async with engine.begin() as connection:
        from sqlalchemy import text

        # Required by the trigram index on ipos.name.
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await connection.run_sync(Base.metadata.create_all)

    yield engine

    await engine.dispose()

    admin = create_async_engine(
        settings.database_url.rpartition("/")[0] + "/postgres",
        isolation_level="AUTOCOMMIT",
    )
    async with admin.connect() as connection:
        from sqlalchemy import text

        await connection.execute(text("DROP DATABASE IF EXISTS ipo_tracker_test"))
    await admin.dispose()


@pytest_asyncio.fixture
async def db_session(test_engine) -> AsyncGenerator[object, None]:
    """A clean session per test, with every table truncated beforehand."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.db.models import Base

    factory = async_sessionmaker(bind=test_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        tables = ", ".join(table.name for table in reversed(Base.metadata.sorted_tables))
        await session.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        await session.commit()
        yield session


@pytest_asyncio.fixture
async def api_client(test_engine) -> AsyncGenerator[object, None]:
    """HTTP client bound to the app, with the DB dependency pointed at tests."""
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.db.models import Base
    from app.db.session import get_db
    from app.main import app

    factory = async_sessionmaker(bind=test_engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as cleaner:
        tables = ", ".join(table.name for table in reversed(Base.metadata.sorted_tables))
        await cleaner.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        await cleaner.commit()

    async def _override_get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()
