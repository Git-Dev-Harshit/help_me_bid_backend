# Deployment

## Docker architecture

PostgreSQL is **not** containerised: the services connect to the instance
already running on the host, reached from inside containers as
`host.docker.internal` (mapped via `extra_hosts: host-gateway`, which also works
on Linux).

One image (`ipo-tracker:latest`) runs three roles; only the command differs, so
API, worker and migrations can never drift apart in their dependencies.

```mermaid
flowchart LR
    PG[("PostgreSQL on the host<br/>host.docker.internal:5432")]
    MIG["migrate<br/>create DB if missing<br/>+ alembic upgrade head<br/>runs once, exits"]
    API["api<br/>uvicorn / gunicorn<br/>:8000"]
    W["worker<br/>APScheduler"]

    MIG --> PG
    MIG -->|service_completed_successfully| API
    MIG -->|service_completed_successfully| W
    API --> PG
    W --> PG
```

| Service | Command | Restart | Health check |
| --- | --- | --- | --- |
| `migrate` | `migrate` | `no` (one-shot) | — |
| `api` | `api` | `unless-stopped` | `GET /health/live`, 30 s interval |
| `worker` | `worker` | `unless-stopped` | disabled — serves no HTTP |

### Database creation

`migrate` waits for the PostgreSQL *server* (connecting to the `postgres`
maintenance database, since the application database may not exist yet), creates
`ipo_tracker` if absent (`app/db/bootstrap.py`), then applies migrations. It is
idempotent — an existing database is left untouched — so start-up needs no
manual SQL even though the server is not managed by compose.

If the configured role may not create databases, the error names the exact
statement to run once by hand.

### Going back to a bundled PostgreSQL container

Add a `postgres` service to `docker-compose.yml`, point `DATABASE_URL` at
`postgres:5432` instead of `host.docker.internal:5432`, and give `migrate`,
`api` and `worker` a `depends_on: postgres: condition: service_healthy`. Nothing
in the application code changes.

### Why a separate `migrate` service

Running migrations inside the API's entrypoint would race when several API
replicas start at once. A one-shot service that both `api` and `worker` wait on
via `service_completed_successfully` makes the ordering explicit and race-free,
and a failed migration stops the deploy instead of leaving a half-migrated
database serving traffic.

### Dockerfile

Multi-stage:

1. **builder** — installs dependencies into `/opt/venv` with `build-essential`
   available for any package lacking a wheel.
2. **runtime** — `python:3.12-slim-bookworm`, copies only the finished virtualenv.
   No compilers ship in the final image.

Hardening applied:

- Runs as the unprivileged `app` user (uid 1001), never root.
- `tini` as PID 1 — reaps zombies and forwards `SIGTERM` for graceful shutdown.
- Only `curl` (health check) and `tini` added at runtime.
- `.dockerignore` keeps `.git`, `.venv`, `.env`, tests and docs out of the context.
- Dependencies pinned exactly in `requirements.txt` for reproducible builds.

---

## Environment variables

Full list with inline commentary: [`.env.example`](../.env.example).

### Application

| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `APP_ENV` | — | `development` | `production` enables hardening checks and hides `/docs` |
| `APP_NAME` | — | `IPO Tracker` | Shown in OpenAPI |
| `APP_TIMEZONE` | — | `Asia/Kolkata` | **Business timezone for all IPO date logic.** Never relies on the server's local zone |
| `LOG_LEVEL` | — | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL` |
| `LOG_FORMAT` | — | `json` | `console` for readable local output |
| `DOCS_ENABLED` | — | unset | Overrides the production default |

### Database

| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `DATABASE_URL` | ✅ | host PostgreSQL | **Must** use `postgresql+asyncpg://`; validated at startup. From a container the host is `host.docker.internal`; running outside Docker, use `localhost` |
| `DB_POOL_SIZE` | — | `5` | Per container. Total = replicas × (pool + overflow) |
| `DB_MAX_OVERFLOW` | — | `10` | Burst capacity |
| `DB_POOL_RECYCLE` | — | `1800` | Keep below any proxy idle timeout |
| `DB_ECHO` | — | `false` | Logs SQL; never enable in production |
| — | | | PostgreSQL runs on the host; there are no `POSTGRES_*` container variables |

### Security

| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `JWT_SECRET_KEY` | ✅ | placeholder | **The app refuses to start when `APP_ENV=production` and this is a known placeholder or under 32 chars.** Generate: `openssl rand -hex 32` |
| `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` | — | `30` | Shorter is safer while there is no refresh token |
| `JWT_ALGORITHM` / `JWT_ISSUER` | — | `HS256` / `ipo-tracker` | |
| `DEFAULT_PHONE_REGION` | — | `IN` | Region for numbers entered without `+country` |
| `PASSWORD_MIN_LENGTH` | — | `8` | |
| `CORS_ORIGINS` | — | empty | Comma-separated. **A wildcard is rejected in production** |
| `RATE_LIMIT_ENABLED` | — | `true` | |
| `RATE_LIMIT_AUTH_PER_MINUTE` | — | `10` | Per container (see caveat below) |

### Scraper and notifications

| Variable | Default | Notes |
| --- | --- | --- |
| `SCRAPER_ENABLED` | `true` | |
| `SCRAPER_SCHEDULE_TIMES` | `09:00,14:00,20:00` | Daily window start times in `APP_TIMEZONE`. Empty switches to interval mode |
| `SCRAPER_SCHEDULE_JITTER_MINUTES` | `30` | Each run fires at a uniformly random moment inside the window |
| `SCRAPER_FAILURE_RETRY_MINUTES` | `20` | One retry after a failed run (`0` disables) |
| `SCRAPER_RUN_ON_STARTUP_IF_EMPTY` | `true` | Scrape at boot only when there is no IPO data yet |
| `SCRAPER_INTERVAL_MINUTES` | `30` | Only used when `SCRAPER_SCHEDULE_TIMES` is empty |
| `SCRAPER_MIN_CONFIDENCE` | `0.5` | Below this a run refuses to write |
| `SCRAPER_RAW_RETENTION_MODE` | `on_failure` | `always` / `on_failure` / `never` |
| `SCRAPER_RAW_RETENTION_DAYS` | `14` | `0` disables pruning |
| `SCRAPER_MAX_RETRIES` | `3` | Transient failures only |
| `NOTIFICATION_ENABLED` | `true` | |
| `NOTIFICATION_INTERVAL_MINUTES` | `15` | How often rules are evaluated |
| `NOTIFICATION_PROVIDER` | `log` | `log` / `fcm` / `webpush` |
| `NOTIFICATION_WINDOW_START_HOUR` / `_END_HOUR` | `8` / `22` | Quiet hours, in `APP_TIMEZONE` |

Provider credentials (`FCM_*`, `VAPID_*`) are only read when that provider is
selected — see [notifications.md](notifications.md).

---

## Local / development

```bash
cp .env.example .env
docker compose up --build
```

The API runs under `uvicorn --reload`, so edits to `app/` restart it
automatically (the source is baked into the image; mount it as a volume if you
want live reload without rebuilding).

---

## Production

### 1. Configuration

```env
APP_ENV=production
LOG_FORMAT=json
DOCS_ENABLED=false
JWT_SECRET_KEY=<openssl rand -hex 32>
CORS_ORIGINS=https://app.example.com
DATABASE_URL=postgresql+asyncpg://user:pass@db-host:5432/ipo_tracker
```

Startup validation refuses to run with a placeholder secret or a wildcard CORS
origin — a misconfigured deploy fails loudly instead of silently insecure.

### 2. Entrypoint behaviour

With `APP_ENV=production` the API runs under gunicorn supervising uvicorn
workers, so a crashed worker is replaced without taking the container down:

```bash
gunicorn app.main:app \
  --worker-class uvicorn.workers.UvicornWorker \
  --workers ${WEB_CONCURRENCY:-4} \
  --bind 0.0.0.0:8000 --timeout 60 --graceful-timeout 30 --keep-alive 5
```

Set `WEB_CONCURRENCY` to roughly `2 × CPU cores`, and size `DB_POOL_SIZE` so
that `replicas × workers × (pool + overflow)` stays under the database's
`max_connections`.

### 3. Hardening the compose file

- Remove the `postgres` port mapping so the database is not exposed.
- Supply secrets through your orchestrator (Docker/Kubernetes secrets), not `.env`.
- Terminate TLS at a reverse proxy or load balancer; the app emits HSTS when
  `APP_ENV=production`.
- Ensure the proxy overwrites `X-Forwarded-For` — the rate limiter treats it as
  a hint, and it is only trustworthy behind a proxy that controls it.

---

## Scaling

### API — stateless, scale freely

```bash
docker compose up -d --scale api=4
```

No session affinity and no correctness-critical in-process state.

**Caveat:** rate limiting is in-process, so `N` replicas allow `N ×` the
configured rate. For a global limit, enforce it at the ingress rather than
adding a shared store here.

### Worker — safe to replicate, rarely necessary

```bash
docker compose up -d --scale worker=2
```

Both jobs take PostgreSQL advisory locks, so overlapping runs skip rather than
duplicate work, and duplicate notifications remain impossible thanks to the
delivery table's unique constraint. One worker comfortably handles a 30-minute
scrape of ~30–100 IPOs; a second is for availability, not throughput.

To drive the jobs from an external scheduler instead (cron, Kubernetes CronJob),
set `SCRAPER_ENABLED=false` / `NOTIFICATION_ENABLED=false` and call:

```bash
docker compose exec worker python -m app.workers.run_once scrape
docker compose exec worker python -m app.workers.run_once notify
```

### Database

Add PgBouncer in transaction mode once total connections approach
`max_connections`. `ipo_snapshots` is the only table that grows without bound —
partition it by month or roll it up if you retain years of history.

---

## Graceful shutdown

`tini` forwards `SIGTERM` to the application.

- **API** — uvicorn/gunicorn stop accepting connections, finish in-flight
  requests, then the lifespan hook disposes the connection pool.
- **Worker** — the scheduler shuts down with `wait=True`, so an in-flight scrape
  finishes rather than being cut off mid-transaction, then the pool is disposed.
  Advisory locks are session-scoped and released automatically even on a hard
  kill, so a crash cannot wedge a job permanently.

Allow at least 30 seconds of termination grace.

---

## Logs

Structured JSON (one object per line) when `LOG_FORMAT=json`, ready for any log
backend without a parsing rule.

```json
{"timestamp":"2026-09-01T17:07:50.221Z","level":"INFO","logger":"app.services.scraper.pipeline",
 "event":"scrape.completed","status":"SUCCESS","strategy":"JSON_API","confidence":1.0,
 "found":30,"valid":30,"inserted":0,"updated":14,"unchanged":16,"duration_ms":605}
```

Events worth alerting on:

| Event | Meaning |
| --- | --- |
| `scrape.low_confidence_abort` | Upstream structure changed; **data was not written** |
| `scrape.failed` | Fetch or extraction failed |
| `scraper.structure_changed` | A field stopped mapping — early warning |
| `notification.send_failed` | A provider rejected a delivery |
| `notification.provider_not_configured` | Falling back to the log sink |
| `request.database_error` | Database trouble |
| `auth.login_failed` | Watch the rate over time for credential stuffing |

**Never logged:** passwords, password hashes, JWTs, push tokens, `Authorization`
headers, VAPID keys. A redaction filter blanks these even if a caller attaches
one by accident.

```bash
docker compose logs -f api
docker compose logs -f worker | grep scrape.completed
```

---

## Monitoring

```bash
curl http://localhost:8000/health        # includes database connectivity
curl http://localhost:8000/health/live   # process liveness only
```

Scraper health lives in the database rather than in a metrics endpoint:

```sql
-- Recent runs
SELECT started_at, status, confidence, records_found,
       ipos_inserted, ipos_updated, error_code
FROM scrape_runs ORDER BY started_at DESC LIMIT 20;

-- Alert if this is stale: nothing succeeded recently
SELECT max(started_at) FROM scrape_runs WHERE status IN ('SUCCESS','PARTIAL');

-- Failed deliveries in the last day
SELECT count(*) FROM notification_deliveries
WHERE status = 'FAILED' AND created_at > now() - interval '1 day';
```

---

## Persistence and backups

Data lives in your host PostgreSQL, so `docker compose down` never touches it —
the containers are disposable, the database is not. `make clean` explicitly
drops the `ipo_tracker` database.

```bash
# Backup (host PostgreSQL, using its own tools)
pg_dump -U postgres -h localhost -Fc ipo_tracker > backup_$(date +%F).dump

# Restore
pg_restore -U postgres -h localhost -d ipo_tracker --clean < backup.dump
```

If `pg_dump` is not on your PATH, run it from a throwaway client container:

```bash
docker run --rm --add-host=host.docker.internal:host-gateway postgres:16-alpine   pg_dump "postgresql://postgres:postgres@host.docker.internal:5432/ipo_tracker" -Fc   > backup.dump
```

What actually needs backing up: `users`, `notification_preferences`, `devices`
and `notification_deliveries` — these cannot be reconstructed. `ipos` refills
itself from the next scrape, and `ipo_snapshots` history is nice to keep but not
critical. Verify restores periodically; an untested backup is not a backup.

---

## Upgrades

```bash
git pull
docker compose up --build -d
```

The `migrate` service applies any new migrations before `api` and `worker`
restart. For zero-downtime deploys, keep migrations backward-compatible with the
previous release (add columns nullable, deploy code, backfill, then tighten in a
later migration).

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `migrate` exits non-zero | Migration failed | `docker compose logs migrate`; fix and re-run |
| API restarts repeatedly | Config validation failed | `docker compose logs api` — usually `JWT_SECRET_KEY` or `CORS_ORIGINS` in production |
| `/health` shows `degraded` | Database unreachable | Check the host PostgreSQL service and `DATABASE_URL` |
| `could not reach the postgres server` | Host PostgreSQL down, or not accepting connections from Docker | Start the service; check `listen_addresses` and `pg_hba.conf` allow the Docker subnet |
| `role may not create databases` | Configured role lacks CREATEDB | Run `CREATE DATABASE ipo_tracker;` once by hand |
| No IPO data | Scraper disabled or failing | `SCRAPER_ENABLED`, then query `scrape_runs` |
| Scrapes fail with 403 | Upstream blocking the client | Check `SCRAPER_USER_AGENT` is browser-like |
| Notifications never arrive | Quiet hours, no device, or nothing eligible | `docker compose logs worker | grep notification` |
| Duplicate notifications | Should be impossible | Verify `uq_notification_delivery_period` exists |
