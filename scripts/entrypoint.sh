#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Container entrypoint.
#
#   api        run the FastAPI service (gunicorn in production, uvicorn with
#              reload during development)
#   worker     run the APScheduler process (scraper + notification jobs)
#   migrate    create the database if needed, apply Alembic migrations, exit
#
# Anything else is executed verbatim, so `docker compose run api pytest` works.
# ---------------------------------------------------------------------------
set -euo pipefail

# Waits for the PostgreSQL *server*, connecting to the `postgres` maintenance
# database. The application database may not exist yet on a first start.
wait_for_server() {
    local attempts="${DB_WAIT_ATTEMPTS:-60}"
    local delay="${DB_WAIT_DELAY:-2}"

    for ((i = 1; i <= attempts; i++)); do
        if python -c "
import asyncio, sys
import asyncpg
from app.core.config import settings
from app.db.bootstrap import _asyncpg_dsn

async def check() -> None:
    connection = await asyncpg.connect(_asyncpg_dsn(settings.maintenance_database_url))
    await connection.close()

try:
    asyncio.run(check())
except Exception:
    sys.exit(1)
" 2>/dev/null; then
            echo "[entrypoint] postgres server is reachable"
            return 0
        fi
        echo "[entrypoint] waiting for postgres server (${i}/${attempts})..."
        sleep "${delay}"
    done

    echo "[entrypoint] could not reach the postgres server in time" >&2
    echo "[entrypoint] check DATABASE_URL, and that the server accepts remote connections" >&2
    return 1
}

# Waits for the application database itself (used by api/worker, which start
# only after the migrate service has created and migrated it).
wait_for_database() {
    local attempts="${DB_WAIT_ATTEMPTS:-60}"
    local delay="${DB_WAIT_DELAY:-2}"

    for ((i = 1; i <= attempts; i++)); do
        if python -c "
import asyncio, sys
from sqlalchemy import text
from app.db.session import SessionFactory

async def check() -> None:
    async with SessionFactory() as session:
        await session.execute(text('SELECT 1'))

try:
    asyncio.run(check())
except Exception:
    sys.exit(1)
" 2>/dev/null; then
            echo "[entrypoint] database is ready"
            return 0
        fi
        echo "[entrypoint] waiting for database (${i}/${attempts})..."
        sleep "${delay}"
    done

    echo "[entrypoint] database did not become ready in time" >&2
    return 1
}

case "${1:-api}" in
    migrate)
        wait_for_server
        echo "[entrypoint] ensuring the application database exists..."
        python -m app.db.bootstrap
        echo "[entrypoint] applying database migrations..."
        alembic upgrade head
        echo "[entrypoint] migrations applied"
        ;;

    api)
        wait_for_database
        if [[ "${APP_ENV:-development}" == "production" ]]; then
            # Gunicorn supervises uvicorn workers: a crashed worker is replaced
            # without dropping the container.
            exec gunicorn app.main:app \
                --worker-class uvicorn.workers.UvicornWorker \
                --workers "${WEB_CONCURRENCY:-4}" \
                --bind "0.0.0.0:${PORT:-8000}" \
                --timeout "${WEB_TIMEOUT:-60}" \
                --graceful-timeout 30 \
                --keep-alive 5 \
                --access-logfile - \
                --error-logfile -
        else
            exec uvicorn app.main:app \
                --host 0.0.0.0 \
                --port "${PORT:-8000}" \
                --reload \
                --reload-dir app
        fi
        ;;

    worker)
        wait_for_database
        exec python -m app.workers.scheduler
        ;;

    *)
        exec "$@"
        ;;
esac
