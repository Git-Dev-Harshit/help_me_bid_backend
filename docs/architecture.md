# Architecture

## Layering

Dependencies point in one direction. A layer may call the one below it, never
the one above.

```
   HTTP request
        ↓
┌───────────────────────────────────────────────────────────┐
│ app/api/          routes, dependencies                    │  no business logic,
│                                                           │  no SQL
├───────────────────────────────────────────────────────────┤
│ app/schemas/      Pydantic request/response models        │  validation, docs
├───────────────────────────────────────────────────────────┤
│ app/services/     business logic                          │  no SQL, no HTTP
├───────────────────────────────────────────────────────────┤
│ app/repositories/ all database access                     │  no scraping,
│                                                           │  no HTTP
├───────────────────────────────────────────────────────────┤
│ app/db/models/    SQLAlchemy ORM                          │
└───────────────────────────────────────────────────────────┘
        ↓
   PostgreSQL
```

`app/core/` (config, security, logging, errors, middleware) and `app/utils/`
(date and parsing helpers) are cross-cutting and may be imported anywhere.

Rules the codebase holds to:

- Route handlers contain no business logic and issue no queries — they bind
  validated input to a service call.
- Repositories contain no scraping or HTTP logic.
- The scraper package never imports from `app/api/`.
- Nothing outside `app/core/config.py` reads `os.environ`.

### One circular-import trap, deliberately avoided

`app/services/scraper/__init__.py` re-exports **nothing**. `pipeline.py` depends
on the repository layer, and the repository layer depends on `models.py` for its
DTOs; eagerly importing the pipeline in the package `__init__` closes that loop.
Submodules are imported directly instead.

---

## Processes

Two containers run from one image, so their dependency sets can never drift.

```mermaid
flowchart LR
    subgraph api["api container"]
        U[uvicorn / gunicorn] --> F[FastAPI app]
    end
    subgraph worker["worker container"]
        S[AsyncIOScheduler] --> SJ["scrape job<br/>3x daily, randomised"]
        S --> NJ[notification job]
    end
    DB[("PostgreSQL<br/>on the host machine")]
    F <--> DB
    SJ --> DB
    NJ --> DB
```

PostgreSQL is not containerised; both services reach the host instance through
`host.docker.internal`.

Scraping and notification evaluation never run inside the API process, so a slow
upstream fetch cannot add latency to a request.

---

## Request flow

```mermaid
sequenceDiagram
    participant C as Client
    participant M as Middleware
    participant R as Route
    participant S as Service
    participant Repo as Repository
    participant DB as PostgreSQL

    C->>M: GET /api/v1/ipos?status=OPEN&min_gmp_percentage=15
    M->>M: request id, rate limit, security headers
    M->>R: dispatch
    R->>R: bind + validate IPOFilterParams
    R->>S: IPOService.list_ipos(filters)
    S->>Repo: list_filtered(filters, today)
    Repo->>DB: SELECT ... WHERE ... ORDER BY ... LIMIT/OFFSET
    Repo->>DB: SELECT count(*) over the same predicates
    DB-->>Repo: rows + total
    Repo-->>S: (rows, total)
    S->>S: derive status, project to response models
    S-->>R: Page[IPOResponse]
    R-->>M: 200
    M-->>C: JSON + X-Request-ID
```

Middleware order (registered last runs outermost):

1. `RequestContextMiddleware` — request id, timing, completion log
2. `SecurityHeadersMiddleware` — nosniff, frame options, CSP, HSTS in production
3. `RateLimitMiddleware` — fixed-window limit on credential endpoints
4. `CORSMiddleware` — only when `CORS_ORIGINS` is set

---

## Scraper architecture

```mermaid
flowchart TD
    A[ScraperHTTPClient<br/>retry + backoff, pooled] --> B[RawPayload]
    B --> C{ExtractorChain}
    C -->|primary| D[JsonApiStrategy]
    C -->|fallback| E[HtmlTableStrategy]
    D --> F[field_map.map_columns<br/>label → token → substring → value shape]
    E --> F
    F --> G[ExtractionResult<br/>records + mapping + confidence]
    G --> H[IPONormalizer<br/>typed canonical values]
    H --> I[IPOValidator<br/>per-record checks]
    I --> J{confidence ≥<br/>SCRAPER_MIN_CONFIDENCE?}
    J -->|yes| K[IPORepository.upsert_many]
    J -->|no| L[Abort: existing data untouched]
    K --> M[(ipos + ipo_snapshots)]
    L --> N[(scrape_runs + scrape_raw_payloads)]
    K --> N
```

Every stage passes a plain dataclass to the next, so each is unit-testable with
no database and no network. Full detail: [scraper.md](scraper.md).

---

## Notification architecture

```mermaid
flowchart TD
    A[Scheduler tick] --> B{NOTIFICATION_ENABLED<br/>and inside window?}
    B -->|no| Z[stop]
    B -->|yes| C[Load enabled rules<br/>joined to active users]
    C --> D[Load IPOs closing today<br/>once, reused across rules]
    D --> E{rule_matches_ipo?<br/>close date · GMP · filters}
    E -->|no| Z
    E -->|yes| F[Claim: INSERT ... ON CONFLICT DO NOTHING<br/>preference_id, ipo_id, period_key]
    F -->|conflict| G[Already sent this window — skip]
    F -->|won| H[COMMIT the claim]
    H --> I[Resolve active devices]
    I -->|none| J[mark SKIPPED]
    I -->|found| K[provider.send]
    K -->|ok| L[mark SENT]
    K -->|error| M[mark FAILED + retire dead tokens]
```

The claim is committed **before** the provider is called, so a crash mid-send
cannot produce a duplicate on the next run. Detail:
[notifications.md](notifications.md).

---

## Authentication flow

```mermaid
sequenceDiagram
    participant C as Client
    participant A as /auth
    participant S as AuthService
    participant DB as PostgreSQL

    C->>A: POST /register {phone_number, password}
    A->>S: register()
    S->>S: normalise phone to E.164, check password policy
    S->>S: hash with Argon2id
    S->>DB: INSERT user
    Note over S,DB: unique constraint is the real arbiter<br/>under concurrent registration
    A-->>C: 201 UserResponse

    C->>A: POST /login {phone_number, password}
    A->>S: authenticate()
    S->>DB: SELECT by normalised phone
    Note over S: unknown account still runs a dummy<br/>verify, equalising response time
    S->>S: verify Argon2 hash, rehash if params changed
    A-->>C: 200 {access_token, token_type, expires_in}

    C->>A: GET /users/me (Authorization: Bearer …)
    A->>S: decode JWT → user id → load active user
    A-->>C: 200 UserResponse
```

Tokens are stateless HS256 JWTs carrying `sub`, `type`, `iat`, `exp`, `iss` and
`jti`. The `jti` claim exists so a revocation list can be added later without a
token-format change. Refresh tokens, OTP login and phone verification all belong
in `AuthService` and need no change to the HTTP layer.

---

## Data flow over an IPO's life

```mermaid
flowchart LR
    S1[Scrape t0<br/>new IPO] -->|INSERT| I[(ipos row)]
    S2[Scrape t1<br/>GMP moved] -->|UPDATE + snapshot| I
    S2 --> H[(ipo_snapshots)]
    S3[Scrape t2<br/>nothing changed] -->|touch last_scraped_at| I
    I --> Q[GET /ipos<br/>status derived from dates]
    I --> N[Notification engine<br/>closing today?]
```

`ipos` holds one row per IPO forever, keyed on `(source, source_ipo_id)`.
Repeated scrapes update in place; a snapshot is appended only when a tracked
value actually changed, so `ipo_snapshots` grows with market activity rather
than with scrape frequency.

---

## Idempotency

Three independent mechanisms, each matched to its failure mode:

| Concern | Mechanism | Why this one |
| --- | --- | --- |
| Duplicate IPO rows | `UNIQUE (source, source_ipo_id)` + upsert | Identity is stable upstream; the constraint makes duplication impossible |
| Duplicate notifications | `UNIQUE (preference_id, ipo_id, period_key)` | Survives concurrent workers, restarts and transaction retries with no lock |
| Overlapping job runs | PostgreSQL advisory locks | Session-scoped: released automatically if the process dies, so a crash cannot wedge the job |

Advisory locks prevent *wasted work*; the unique constraints prevent *wrong
results*. The system stays correct even if the locks are removed.

---

## Error handling

Every deliberate failure derives from `AppError` and carries a stable `code`.
`app/core/errors.py` registers handlers for:

| Exception | Response |
| --- | --- |
| `AppError` subclasses | Their own status and code |
| `RequestValidationError` | `422 VALIDATION_ERROR` with per-field details |
| `StarletteHTTPException` | Normalised into the shared envelope |
| `IntegrityError` | `409 CONFLICT`, SQL never exposed |
| `SQLAlchemyError` | `503 DATABASE_UNAVAILABLE`, logged with traceback |
| Anything else | `500 INTERNAL_ERROR`, logged with traceback |

Clients always receive:

```json
{"success": false, "error": {"code": "…", "message": "…", "details": {…}}}
```

Stack traces, SQL and driver messages are logged internally and never returned.

---

## Scalability

The API is stateless: no session affinity, no in-process caches that affect
correctness. Multiple replicas can sit behind a load balancer immediately.

```bash
docker compose up -d --scale api=4
```

| Concern | Current approach | When it needs changing |
| --- | --- | --- |
| API throughput | Stateless replicas; gunicorn + uvicorn workers in production | — |
| Worker duplication | Advisory locks + unique constraints make replicas safe | — |
| Database connections | Per-container pool (`DB_POOL_SIZE` + overflow) | Add PgBouncer past ~100 total connections |
| Rate limiting | In-process, therefore **per container** | Enforce at the ingress/load balancer for a global limit |
| Query cost | Every filter is SQL; only one page is materialised | — |
| N+1 queries | Rules joined to users in one query; IPOs closing today loaded once and reused across rules | — |
| Snapshot growth | Written only on real change | Partition or roll up `ipo_snapshots` if retention grows |
| Raw payloads | Retained per policy, pruned on a schedule | Move to object storage if `always` retention is needed long-term |

### Performance choices already made

- Async end to end: FastAPI, SQLAlchemy async, asyncpg, httpx.
- One pooled `httpx.AsyncClient` reused across scrapes (TLS handshakes amortised).
- `pool_pre_ping` so a connection dropped by the server reconnects instead of 500ing.
- Composite index `(close_date, gmp_percentage)` serving the notification query.
- GIN + `pg_trgm` index backing `?search=`.
- Bulk upsert: one `SELECT` for existing rows regardless of batch size.
- `eager_defaults` so server-generated timestamps come back via `RETURNING`
  rather than a second round trip.
