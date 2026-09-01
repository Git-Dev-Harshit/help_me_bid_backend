# Development

The application runs in Docker; PostgreSQL runs on your host machine. You do not
need Python or any Python dependency installed locally — including to run the
tests.

The `ipo_tracker` database is created automatically on first start
(`app/db/bootstrap.py`), so there is no manual `CREATE DATABASE` step.

```bash
cp .env.example .env
make up          # or: docker compose up --build -d
make logs        # watch everything
```

The API runs under `uvicorn --reload`. `make help` lists every task; each recipe
shows the underlying `docker compose` command if you do not have `make`.

---

## Project layout

```
app/
├── main.py            app factory, middleware, health endpoints
├── api/
│   ├── deps.py        session + current-user dependencies
│   └── v1/            route modules, one per resource
├── core/              config, security, logging, errors, middleware, exceptions
├── db/
│   ├── base.py        declarative base, mixins, naming conventions
│   ├── enums.py       shared enumerations
│   ├── models/        ORM models
│   └── session.py     async engine + session factory
├── repositories/      ALL database access
├── schemas/           Pydantic request/response models
├── services/          business logic
│   ├── scraper/       client, extractor, field_map, normalizer, validator, pipeline
│   └── notifications/ engine, providers, service
├── workers/           scheduler + job entrypoints
└── utils/             date and parsing helpers
```

Conventions worth keeping:

- No business logic in route handlers; no SQL outside `repositories/`.
- No scraper logic in repositories; no HTTP concerns in services.
- Nothing outside `core/config.py` reads `os.environ`.
- Type hints everywhere; docstrings on public classes and non-obvious functions.
- Comments explain **why**, not what.

---

## Adding a new API endpoint

1. **Schemas** — request/response models in `app/schemas/`.

2. **Repository** — any new query in `app/repositories/`.

3. **Service** — business logic in `app/services/`, returning schema objects.

4. **Route** — bind and delegate in `app/api/v1/<resource>.py`:

   ```python
   @router.get(
       "/things/{thing_id}",
       response_model=ThingResponse,
       summary="Get a thing",
       description="Longer explanation shown in /docs.",
       responses={404: {"model": ErrorResponse, "description": "Not found."}},
   )
   async def get_thing(
       thing_id: Annotated[uuid.UUID, Path(description="Thing identifier.")],
       user: CurrentUser,          # omit for a public endpoint
       session: DbSession,
   ) -> ThingResponse:
       return await ThingService(session).get(thing_id)
   ```

5. **Register** the router in `app/api/v1/router.py` if the module is new.

6. **Test** it in `tests/integration/`.

Always pass `summary`, `description`, `response_model` and `responses` — they
are what make `/docs` useful to the frontend and Flutter teams.

> A route returning `204` must also pass `response_model=None`: a `-> None`
> return annotation is otherwise inferred as a response model, which FastAPI
> rejects on a body-less status.

---

## Adding a new filter to `GET /api/v1/ipos`

Two edits:

1. A field on `IPOFilterParams` (`app/schemas/ipo.py`) — FastAPI publishes it as
   a documented query parameter automatically.
2. A clause in `IPORepository._apply_filters` (`app/repositories/ipo.py`).

```python
# schemas/ipo.py
min_rating: Annotated[int | None, Field(ge=1, le=5)] = None

# repositories/ipo.py, inside _apply_filters
if filters.min_rating is not None:
    statement = statement.where(IPO.rating >= filters.min_rating)
```

Add an index if the filter will be common. Routes, services and response models
need no change.

To make a field **sortable**, add it to `IPOSortField` and to `SORT_COLUMNS`.
Never interpolate a user-supplied column name into SQL — the enum and the
mapping are the whole defence.

---

## Adding a database model

1. Create `app/db/models/<name>.py`, inheriting `Base` plus the mixins you need
   (`UUIDPrimaryKeyMixin`, `TimestampMixin`).
2. Export it from `app/db/models/__init__.py` — **required**, or Alembic will not
   see it and will happily generate a migration that drops nothing and creates
   nothing.
3. Generate and review a migration.

```bash
make migration m="add things table"
```

---

## Migrations

```bash
make migration m="add ipos.sector"   # autogenerate
make migrate                          # apply (also automatic on startup)
```

Under the hood:

```bash
docker compose run --rm -v "$PWD/alembic/versions:/app/alembic/versions" \
  migrate alembic revision --autogenerate -m "add ipos.sector"
docker compose run --rm migrate
```

The volume mount matters — without it the file is written inside the container
and lost.

**Always review the generated file.** Alembic cannot infer data migrations, and
it renders a column rename as a `DROP` plus an `ADD`, which loses data. For a
rename, replace the generated pair with `op.alter_column(..., new_column_name=…)`.

Other useful commands:

```bash
docker compose run --rm migrate alembic current
docker compose run --rm migrate alembic history --verbose
docker compose run --rm migrate alembic downgrade -1
docker compose run --rm migrate alembic upgrade head --sql   # print SQL only
```

---

## Adding a scraper field

Covered in detail in [scraper.md](scraper.md#adding-support-for-a-new-upstream-field).
In short: a new upstream column is already captured in `raw_data` from the first
scrape. To promote it to a real column, add a `CanonicalField` in `field_map.py`,
a column on `IPO`, mapping in `NormalizedIPO`, an entry in `TRACKED_FIELDS`, and
a migration.

Adding an **alias** for an existing field — the usual response to an upstream
rename — is a one-line change:

```python
CanonicalField(
    name="close_date",
    aliases=("srt close", "close", "close date", "closing date", "last date",
             "issue close", "end date", "bidding close"),   # ← new alias
    ...
)
```

---

## Adding a notification provider

1. Subclass `NotificationProvider` in `app/services/notifications/providers.py`.
2. Implement `is_configured` and `send`, importing any third-party client lazily
   inside `send` so it stays out of the base image.
3. Register it in `_PROVIDERS`.
4. Add its name to `NotificationProviderName` in `app/core/config.py`, and to
   `NotificationChannel` in `app/db/enums.py` if it is a new transport.

Nothing in the engine, routes or database changes.

---

## Running tests

```bash
make test              # all 273
make test-unit         # no database required
make test-integration
make coverage
```

Or directly:

```bash
docker compose -f docker-compose.yml -f docker-compose.test.yml run --rm test
docker compose -f docker-compose.yml -f docker-compose.test.yml run --rm test \
  pytest tests/unit/test_parsing.py -v
```

### How the suite is organised

| Location | Needs a database | Covers |
| --- | --- | --- |
| `tests/unit/test_parsing.py` | no | Value parsers: numbers, percentages, dates, price bands, amounts |
| `tests/unit/test_scraper_resilience.py` | no | Column mapping and the six HTML structure variants |
| `tests/unit/test_normalizer.py` | no | Cell → canonical value, against the real captured payload |
| `tests/unit/test_validator.py` | no | Record validation and confidence scoring |
| `tests/unit/test_notification_rules.py` | no | Eligibility rules and interval bucketing |
| `tests/unit/test_security.py` | no | Argon2, JWT, phone normalisation |
| `tests/integration/test_auth_api.py` | yes | Registration, login, protected endpoints, error envelope |
| `tests/integration/test_ipo_api.py` | yes | Upsert, every filter, sorting, pagination |
| `tests/integration/test_notifications.py` | yes | Eligibility and **deduplication** end to end |
| `tests/integration/test_scrape_pipeline.py` | yes | Persistence and the failure policy |
| `tests/integration/test_rate_limit.py` | yes | Credential-endpoint throttling |
| `tests/unit/test_scheduler.py` | no | Scrape windows, randomised fire times, job wiring |

Integration tests create and drop their own `ipo_tracker_test` database on your
host PostgreSQL, so they never touch development data.

**No test contacts the live InvestorGain site.** Scraper tests replay fixtures
from `tests/fixtures/`, so the suite is deterministic and works offline.

### Two things to know before writing tests

- **Rate limiting is disabled** in the test compose service. The limiter keeps
  in-process state, so leaving it on would make results depend on test order;
  `test_rate_limit.py` enables it explicitly for its own cases.
- **`SCRAPER_ENABLED=false`** in the test service, so no suite can reach the live
  source. Tests that exercise the scheduler force it back on via the
  `scraper_scheduled` fixture rather than reading ambient config.
- **Event loops.** The engine fixture is session-scoped and uses `NullPool`, so
  each session opens its own connection on the loop that is actually running.
  A pooled asyncpg connection handed to a different event loop raises
  `got Future attached to a different loop`. Keep per-test fixtures
  function-scoped.

---

## Linting and formatting

```bash
make lint      # ruff check + mypy
make format    # ruff check --fix, in place
```

Configuration lives in `pyproject.toml`: ruff with `E,F,W,I,N,UP,B,C4,SIM,RUF`,
100-character lines, `py312` target.

---

## Debugging scraper failures

1. **Check recent runs.**

   ```bash
   make psql
   ```
   ```sql
   SELECT started_at, status, strategy, confidence,
          records_found, records_valid, error_code, warnings
   FROM scrape_runs ORDER BY started_at DESC LIMIT 10;
   ```

2. **Read the retained payload** — kept automatically for failed runs.

   ```sql
   SELECT p.content FROM scrape_raw_payloads p
   JOIN scrape_runs r ON r.id = p.scrape_run_id
   WHERE r.status = 'FAILED' ORDER BY p.captured_at DESC LIMIT 1;
   ```

3. **Compare field mappings** — a field that stopped mapping is the signal.

   ```sql
   SELECT started_at, field_mapping FROM scrape_runs
   WHERE status IN ('SUCCESS','PARTIAL') ORDER BY started_at DESC LIMIT 2;
   ```

4. **Reproduce offline.** Save the payload as a fixture and write a failing test:

   ```python
   def test_new_upstream_shape(load_html):
       result = HtmlTableStrategy().extract(make_payload(load_html("new_shape.html")))
       assert result.records
   ```

5. **Run one scrape by hand** with debug logging:

   ```bash
   docker compose exec -e LOG_LEVEL=DEBUG worker python -m app.workers.run_once scrape
   ```

The fix is usually an **alias**, not new parsing logic.

## Debugging notifications

```bash
# Force an evaluation now
make notify

# Open the quiet-hours window for a one-off run
docker compose exec -e NOTIFICATION_WINDOW_END_HOUR=24 worker \
  python -m app.workers.run_once notify
```

The summary line tells you exactly where a notification was lost:

```
notification.evaluation_completed rules=1 ipos=4 matches=2 claimed=2
  duplicates_skipped=0 sent=2 failed=0 skipped_no_device=0
```

| Reading | Meaning |
| --- | --- |
| `rules=0` | No enabled rule on an active user |
| `ipos=0` | Nothing closes today |
| `matches=0` | Rules exist but no IPO cleared the criteria |
| `duplicates_skipped>0` | Already sent this interval — working as designed |
| `skipped_no_device>0` | Eligible, but the user has no active device |

---

## Useful one-liners

```bash
docker compose exec worker python -m app.workers.run_once scrape
docker compose exec api python -c "from app.core.config import settings; print(settings.model_dump(exclude={'jwt_secret_key'}))"
docker compose exec worker python -m app.workers.run_once schedule   # next fire times
docker compose logs -f worker | grep -E "scrape\.|notification\."
make clean && docker compose up --build              # full reset (drops the database)
```
