# API Reference

Base URL: `http://localhost:8000`  ·  Version prefix: `/api/v1`

Interactive documentation is generated from the code and is always current:

| URL | Purpose |
| --- | --- |
| `/docs` | Swagger UI (try requests in the browser) |
| `/redoc` | ReDoc reference |
| `/openapi.json` | Machine-readable schema, for client generation |

Disabled automatically when `APP_ENV=production` unless `DOCS_ENABLED=true`.

---

## Conventions

**Authentication.** Protected endpoints require a bearer token:

```
Authorization: Bearer <access_token>
```

**Errors.** Every failure returns one envelope:

```json
{
  "success": false,
  "error": {
    "code": "IPO_NOT_FOUND",
    "message": "IPO not found",
    "details": null
  }
}
```

Branch on `error.code` (stable); `error.message` is human-facing and may change.

**Common codes**

| Code | Status | Meaning |
| --- | --- | --- |
| `VALIDATION_ERROR` | 422 | Payload or query failed validation; `details` maps field → message |
| `INVALID_CREDENTIALS` | 401 | Wrong phone number or password (deliberately indistinguishable) |
| `INVALID_TOKEN` | 401 | Missing, malformed or expired token |
| `USER_INACTIVE` | 403 | Account deactivated |
| `PHONE_ALREADY_REGISTERED` | 409 | Phone number already in use |
| `WEAK_PASSWORD` | 422 | Password policy not met |
| `INVALID_PHONE_NUMBER` | 422 | Phone number could not be parsed |
| `IPO_NOT_FOUND` | 404 | No such IPO |
| `NOTIFICATION_PREFERENCE_NOT_FOUND` | 404 | No such rule (or not yours) |
| `DEVICE_NOT_FOUND` | 404 | No such device (or not yours) |
| `RATE_LIMITED` | 429 | Too many auth attempts; see `Retry-After` |
| `DATABASE_UNAVAILABLE` | 503 | Database error (details logged, never returned) |
| `INTERNAL_ERROR` | 500 | Unexpected error (details logged, never returned) |

Every response carries an `X-Request-ID` header, echoed from the request if you
supply one — useful for correlating with server logs.

---

# Authentication

## `POST /api/v1/auth/register`

Create an account. **No authentication.**

**Body**

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `phone_number` | string | ✅ | Any common format; normalised to E.164 |
| `password` | string | ✅ | 8–128 chars, at least one letter and one digit |
| `name` | string | — | Max 120 chars |
| `email` | string | — | Valid address; lower-cased before storage |

Phone numbers are normalised, so `9876543210`, `+91 98765 43210` and
`098765 43210` all refer to the same account. The default region comes from
`DEFAULT_PHONE_REGION` (`IN`).

**Request**

```bash
curl -X POST http://localhost:8000/api/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"phone_number":"9876543210","password":"Str0ngPass1","name":"Asha Menon"}'
```

**`201 Created`**

```json
{
  "id": "c0155a98-b545-4d9b-b700-7e593ee8e95e",
  "phone_number": "+919876543210",
  "name": "Asha Menon",
  "email": null,
  "is_active": true,
  "created_at": "2026-09-01T17:08:14.198352Z",
  "last_login_at": null
}
```

**Errors** — `409 PHONE_ALREADY_REGISTERED`, `422 WEAK_PASSWORD`,
`422 INVALID_PHONE_NUMBER`, `429 RATE_LIMITED`.

---

## `POST /api/v1/auth/login`

Exchange credentials for an access token. **No authentication.**

**Body** — `phone_number` (string, required), `password` (string, required).

```bash
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"phone_number":"9876543210","password":"Str0ngPass1"}'
```

**`200 OK`**

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer",
  "expires_in": 1800
}
```

`expires_in` is seconds (`JWT_ACCESS_TOKEN_EXPIRE_MINUTES`, default 30).

**Errors** — `401 INVALID_CREDENTIALS` (identical whether the account is unknown
or the password is wrong, so this endpoint cannot enumerate registered numbers),
`403 USER_INACTIVE`, `429 RATE_LIMITED`.

---

# Users

## `GET /api/v1/users/me` 🔒

Returns the authenticated account. Same shape as the register response.

## `PATCH /api/v1/users/me` 🔒

Update `name` and/or `email`. Omitted fields are unchanged. The phone number is
the login identity and cannot be changed here.

---

# IPOs

## `GET /api/v1/ipos`

List IPOs with filtering, search, sorting and pagination. **No authentication.**

All filters compose and are applied in SQL — no result set is ever filtered in
application memory.

### Query parameters

**Classification**

| Parameter | Type | Values |
| --- | --- | --- |
| `status` | enum | `UPCOMING`, `OPEN`, `CLOSING_TODAY`, `CLOSED`, `LISTED`, `UNKNOWN` |
| `ipo_type` | enum | `MAINBOARD`, `SME`, `UNKNOWN` |
| `exchange` | enum | `NSE`, `BSE`, `NSE_SME`, `BSE_SME`, `NSE_BSE`, `UNKNOWN` |

`status` is **derived per request** from the IPO's dates against today in
`APP_TIMEZONE`, so it is never stale:

```
listing_date <= today                          → LISTED
close_date   == today                          → CLOSING_TODAY
close_date   <  today                          → CLOSED
open_date    <= today <= close_date            → OPEN
open_date    >  today                          → UPCOMING
```

**Dates** — each of `open_date`, `close_date`, `listing_date` supports three
parameters:

| Parameter | Type | Notes |
| --- | --- | --- |
| `{field}` | ISO date **or shortcut** | `today`, `tomorrow`, `yesterday`, `this_week`, `next_week` |
| `{field}_from` | ISO date | Inclusive lower bound |
| `{field}_to` | ISO date | Inclusive upper bound |

Weeks run Monday–Sunday.

**Numeric ranges**

| Parameter | Applies to |
| --- | --- |
| `min_gmp`, `max_gmp` | Grey-market premium (₹) |
| `min_gmp_percentage`, `max_gmp_percentage` | GMP as a percentage |
| `min_price`, `max_price` | Issue price band (overlap semantics — see below) |
| `min_lot_size`, `max_lot_size` | Lot size |
| `min_issue_size`, `max_issue_size` | Issue size in crore |
| `min_subscription`, `max_subscription` | Subscription multiple |

Price is a *band*, so `min_price=100` means "the top of the band reaches 100" and
`max_price=500` means "the bottom of the band stays under 500" — an IPO priced
100–110 matches both.

**Search, sort, pagination**

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `search` | string (≤100) | — | Case-insensitive substring over name and symbol; `%` and `_` are escaped |
| `sort_by` | enum | `close_date` | `name`, `ipo_name`, `open_date`, `close_date`, `listing_date`, `gmp`, `gmp_percentage`, `price`, `lot_size`, `issue_size`, `subscription`, `created_at`, `updated_at` |
| `sort_order` | enum | `desc` | `asc`, `desc` |
| `page` | int ≥ 1 | `1` | |
| `page_size` | int 1–100 | `20` | Values above 100 are rejected with `422` |

`sort_by` is validated against a closed whitelist and mapped to a column object —
user input never reaches SQL as a column name. `NULL`s always sort last, and `id`
breaks ties so pagination cannot repeat or skip a row.

### Examples

```http
GET /api/v1/ipos?status=OPEN
GET /api/v1/ipos?min_gmp_percentage=15
GET /api/v1/ipos?ipo_type=SME&exchange=NSE_SME
GET /api/v1/ipos?close_date=today
GET /api/v1/ipos?close_date_from=2026-09-01&close_date_to=2026-09-30
GET /api/v1/ipos?min_gmp=50&max_gmp=200
GET /api/v1/ipos?search=jewellers
GET /api/v1/ipos?sort_by=gmp_percentage&sort_order=desc&page=1&page_size=20
```

Combined — *upcoming SME IPOs with GMP ≥ 15%, closing this week, highest GMP first*:

```http
GET /api/v1/ipos?status=UPCOMING&ipo_type=SME&min_gmp_percentage=15
    &close_date_from=2026-09-01&close_date_to=2026-09-07
    &sort_by=gmp_percentage&sort_order=desc&page=1&page_size=20
```

### `200 OK`

```json
{
  "items": [
    {
      "id": "0bef05f0-3282-4167-b8ba-92aa5143be34",
      "name": "Deepa Jewellers",
      "symbol": null,
      "ipo_type": "MAINBOARD",
      "exchange": "NSE_BSE",
      "status": "CLOSING_TODAY",
      "open_date": "2026-09-01",
      "close_date": "2026-09-03",
      "allotment_date": "2026-09-04",
      "listing_date": "2026-09-08",
      "price": { "min": 177, "max": 177 },
      "lot_size": 84,
      "issue_size_crore": 459.72,
      "gmp": 44,
      "gmp_percentage": 24.86,
      "gmp_band": { "low": 44, "high": 55 },
      "estimated_listing_price": 221,
      "subscription_times": 0.88,
      "rating": 4,
      "pe_ratio": null,
      "has_anchor_investors": true,
      "detail_url": "https://www.investorgain.com/gmp/deepa-jewellers-ipo/2081/",
      "updated_at": "2026-09-01T10:30:00Z"
    }
  ],
  "pagination": {
    "page": 1,
    "page_size": 20,
    "total_items": 125,
    "total_pages": 7,
    "has_next": true,
    "has_previous": false
  }
}
```

**Errors** — `422 VALIDATION_ERROR` for an unknown `sort_by`, a `page_size`
above 100, an unparseable date, or an inverted range (`min` above `max`).

---

## `GET /api/v1/ipos/filters`

Filter values a client can offer, so a UI need not hardcode them. IPO types and
exchanges come from the live dataset; statuses and sortable fields are part of
the API contract. **No authentication.**

```json
{
  "ipo_types": ["MAINBOARD", "SME"],
  "exchanges": ["BSE_SME", "NSE_BSE", "NSE_SME"],
  "statuses": ["UPCOMING", "OPEN", "CLOSING_TODAY", "CLOSED", "LISTED", "UNKNOWN"],
  "sort_fields": ["name", "open_date", "close_date", "listing_date", "gmp", "gmp_percentage", "price", "lot_size", "issue_size", "subscription", "created_at", "updated_at"],
  "gmp_percentage_range": { "min": -5.05, "max": 110.00 },
  "price_range": { "min": 50, "max": 988 }
}
```

---

## `GET /api/v1/ipos/{ipo_id}`

Full detail for one IPO. **No authentication.**

Returns everything in the list shape, plus provenance:

| Field | Notes |
| --- | --- |
| `source` | Upstream source identifier (`investorgain`) |
| `source_status` | Raw upstream code (`U`, `O`, `C`, `LP`, `LN`) — provenance only |
| `raw_data` | Source fields with no canonical column yet, preserved verbatim |
| `first_seen_at` | When this IPO was first scraped |
| `last_scraped_at` | Most recent scrape that saw it |
| `data_changed_at` | Most recent scrape where a tracked value actually changed |

**Errors** — `404 IPO_NOT_FOUND`, `422` for a malformed UUID.

---

## `GET /api/v1/ipos/{ipo_id}/history` 🔒

Recorded changes for one IPO, newest first — GMP movement over the issue's life,
subscription progress, date revisions.

**Query** — `limit` (int 1–200, default 50).

```json
[
  {
    "captured_at": "2026-09-01T17:07:50Z",
    "gmp": 44,
    "gmp_percentage": 24.86,
    "subscription_times": 0.88,
    "changed_fields": {
      "gmp": { "old": 25.0, "new": 44.0 },
      "gmp_percentage": { "old": 14.12, "new": 24.86 }
    }
  }
]
```

---

# Notification Preferences

All endpoints require authentication and operate only on the caller's own rules.
Another user's rule returns `404`, not `403`, so ids cannot be probed.

## `GET /api/v1/notification-preferences` 🔒

Returns an array of the caller's rules.

## `POST /api/v1/notification-preferences` 🔒

Create a rule. A notification is sent only when **all** hold:

1. the IPO's `close_date` is today in `APP_TIMEZONE` (unless `only_on_close_date` is `false`);
2. its GMP percentage is within `[min_gmp_percentage, max_gmp_percentage]`;
3. the current time is inside the server's notification window;
4. `interval_minutes` has elapsed since the last alert for that IPO;
5. no notification already exists for that interval.

**Body**

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `label` | string (≤80) | `null` | Your own name for the rule |
| `is_enabled` | bool | `true` | Disabled rules are not evaluated |
| `min_gmp_percentage` | decimal | `0` | −100…1000, inclusive |
| `max_gmp_percentage` | decimal | `null` | Optional upper bound |
| `interval_minutes` | int | `180` | **15–10080**; also the dedup window |
| `only_on_close_date` | bool | `true` | The closing-date restriction |
| `ipo_types` | array | `null` | `MAINBOARD`, `SME`; null/empty = all |
| `exchanges` | array | `null` | `NSE`, `BSE`, `NSE_SME`, `BSE_SME`, `NSE_BSE`; null/empty = all |
| `min_subscription_times` | decimal | `null` | Optional demand floor |
| `channels` | array | `["PUSH"]` | `PUSH`, `WEBPUSH`, `LOG` |
| `extra_conditions` | object | `{}` | Forward-compatible criteria store |

*"Alert me every 3 hours about IPOs closing today with GMP ≥ 15%"*:

```bash
curl -X POST http://localhost:8000/api/v1/notification-preferences \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"label":"High GMP closing today","min_gmp_percentage":15,
       "interval_minutes":180,"only_on_close_date":true,"channels":["PUSH"]}'
```

**`201 Created`** returns the stored rule, including `id`, `user_id`,
`created_at`, `updated_at` and `last_evaluated_at`.

**Errors** — `422 VALIDATION_ERROR` (e.g. `interval_minutes` below 15, or
`max_gmp_percentage` below `min_gmp_percentage`).

## `GET /api/v1/notification-preferences/{id}` 🔒

## `PUT /api/v1/notification-preferences/{id}` 🔒

Partial update — only the fields present in the body change.

```bash
curl -X PUT http://localhost:8000/api/v1/notification-preferences/$ID \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"min_gmp_percentage": 25}'
```

## `DELETE /api/v1/notification-preferences/{id}` 🔒

`204 No Content`. Past deliveries for the rule are removed with it.

---

## `GET /api/v1/notifications` 🔒

Paginated delivery history, newest first. Query: `page`, `page_size`.

```json
{
  "items": [
    {
      "id": "…",
      "ipo_id": "…",
      "ipo_name": "ESDS Software Solution",
      "preference_id": "…",
      "channel": "PUSH",
      "status": "SENT",
      "business_date": "2026-09-01",
      "gmp_percentage_at_send": 63.40,
      "sent_at": "2026-09-01T17:09:32Z",
      "created_at": "2026-09-01T17:09:32Z",
      "error_message": null
    }
  ],
  "pagination": { "page": 1, "page_size": 20, "total_items": 2, "total_pages": 1, "has_next": false, "has_previous": false }
}
```

`status` is one of `PENDING`, `SENT`, `FAILED`, `SKIPPED` (eligible but the user
had no active device on that channel).

---

# Devices

## `POST /api/v1/devices` 🔒

Register a push target — an FCM token for Android/iOS/Flutter, or a Web Push
subscription for browsers.

**Body** — `device_type` (`ANDROID` | `IOS` | `WEB`, required), `push_token`
(string 8–4096, required), `device_name`, `app_version`.

Registering an existing `push_token` is **idempotent**: the token is reassigned
to the calling user and reactivated, which is what provider token rotation
requires. Push tokens are never returned or logged.

```bash
curl -X POST http://localhost:8000/api/v1/devices \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"device_type":"ANDROID","push_token":"<fcm-token>","device_name":"Pixel 8"}'
```

**`201 Created`**

```json
{
  "id": "20a612de-c5b8-47d1-9c6a-1c49ff00dbbf",
  "device_type": "ANDROID",
  "device_name": "Pixel 8",
  "app_version": "1.0.0",
  "is_active": true,
  "created_at": "2026-09-01T17:09:02Z",
  "last_seen_at": "2026-09-01T17:09:02Z"
}
```

## `GET /api/v1/devices` 🔒

## `DELETE /api/v1/devices/{device_id}` 🔒

Deactivates the device. `204 No Content`. Errors: `404 DEVICE_NOT_FOUND`.

---

# Health

## `GET /health`

```json
{"status": "ok", "version": "1.0.0", "environment": "development", "database": "ok"}
```

`status` is `degraded` and `database` is `unavailable` when the database cannot
be reached. Always returns `200`; inspect the body.

## `GET /health/live`

Liveness probe — `{"status": "alive"}` whenever the process is running. Checks no
dependencies, so it never fails because of a database blip. Used by the
container health check.

---

# Notes for client developers

- **Versioning.** Everything lives under `/api/v1`. A future `/api/v2` mounts
  alongside it; v1 clients keep working.
- **Generate a client.** `/openapi.json` works with `openapi-generator`,
  `swagger-codegen`, or `dart-openapi` for Flutter.
- **Money and percentages** are JSON numbers with two decimal places; parse them
  as decimals, not floats, if you do arithmetic on them.
- **Timestamps** are ISO-8601 UTC. **Dates** (`open_date`, `close_date`,
  `listing_date`, `business_date`) are plain `YYYY-MM-DD` in `APP_TIMEZONE` —
  do not shift them into the device's local timezone.
- **`null` means unknown**, not zero. A `gmp` of `null` means no premium has been
  reported; a `gmp` of `0` means the reported premium is zero.
- **Poll efficiently.** `updated_at` changes on every scrape that touched the
  row; `data_changed_at` (detail view) changes only when a tracked value moved.
- **CORS** must list your frontend's origin in `CORS_ORIGINS`.
