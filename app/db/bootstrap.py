"""Create the application database if it does not exist yet.

With the bundled PostgreSQL container, `POSTGRES_DB` created the database for
us.  Pointing at an existing server (a local install, or a managed instance)
removes that, and the first `alembic upgrade` would fail with
"database ... does not exist".

Running this before migrations keeps the promise that `docker compose up` needs
no manual SQL.  It is idempotent and safe to run on every start-up: an existing
database is left completely untouched.

Requires a role permitted to create databases.  If it is not, the error message
says exactly which statement to run by hand.
"""

from __future__ import annotations

import asyncio

import asyncpg
from sqlalchemy.engine import make_url

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def _asyncpg_dsn(url: str) -> str:
    """Convert a SQLAlchemy URL into the plain DSN asyncpg expects."""
    return make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)


async def ensure_database() -> bool:
    """Create the configured database when missing.

    Returns ``True`` if it was created, ``False`` if it already existed.
    """
    database = settings.database_name
    if not database:
        raise RuntimeError("DATABASE_URL does not name a database")

    # CREATE DATABASE cannot run inside a transaction, so connect directly with
    # asyncpg (autocommit) rather than through the SQLAlchemy engine.
    connection = await asyncpg.connect(_asyncpg_dsn(settings.maintenance_database_url))
    try:
        exists = await connection.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", database
        )
        if exists:
            logger.info("bootstrap.database_present", extra={"database": database})
            return False

        # The identifier cannot be parameterised; quote it instead. The value
        # comes from our own configuration, never from user input.
        await connection.execute(f'CREATE DATABASE "{database}"')
        logger.info("bootstrap.database_created", extra={"database": database})
        return True
    except asyncpg.InsufficientPrivilegeError as exc:
        raise RuntimeError(
            f"The configured role may not create databases. Create it once by hand:\n"
            f'    CREATE DATABASE "{database}";'
        ) from exc
    except asyncpg.DuplicateDatabaseError:
        # Another container won the race between the check and the CREATE.
        logger.info("bootstrap.database_present", extra={"database": database})
        return False
    finally:
        await connection.close()


def main() -> int:
    from app.core.logging import configure_logging

    configure_logging()
    asyncio.run(ensure_database())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
