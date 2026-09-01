# Scraper

## Target and the finding that shaped the design

Target page: <https://www.investorgain.com/report/ipo-gmp-live/331/>

**The page is a client-rendered Next.js application.** Its server HTML contains
an empty table:

```html
<table class="report-data-table" id="reportTable">
  <thead id="tableHead"><tr></tr></thead>
  <tbody id="tableBody">
    <tr><td colSpan="1" style="text-align:center">No data available</td></tr>
  </tbody>
</table>
```

A BeautifulSoup scrape of that HTML returns **zero IPO rows**. There are only
two ways to get the data:

| Option | Cost | Chosen |
| --- | --- | --- |
| Headless browser (Playwright) to render, then parse the DOM | +450 MB image, ~4–8 s and ~300 MB RAM per scrape | ✗ |
| Call the JSON feed the page's own JavaScript calls | ~0.3 s, no browser, machine-readable fields | ✓ |

The feed was found by reading the page's chunk bundles, which contain:

```js
O.get("cloud/v2/report/data-read/" + e.reportInfo[0].id + "/" + page + "/" + month + …)
```

against base URL `https://webnodejs.investorgain.com/`.

The site also answers **403 to requests without browser-like headers**, so the
client sends a realistic `User-Agent`, `Referer` and `Origin`.

### The endpoint

```
GET {SCRAPER_API_BASE_URL}/cloud/v2/report/data-read/
    {report_id}/{page}/{month}/{year}/{financial_year}/{sort}/{param}?search=&v=1
```

- `report_id` — `331` (`SCRAPER_REPORT_ID`)
- `financial_year` — Indian FY, April–March, e.g. `2026-27`
- `param` — must be the literal `all`; any other value returns `msg: -1` with no rows

Built by `build_report_api_url()` in `app/services/scraper/client.py`.

### What the feed returns

Display columns carry HTML; columns prefixed `~` are pre-parsed machine values.

| Column | Example | Maps to |
| --- | --- | --- |
| `~id` | `2081` | `source_ipo_id` — **stable identity** |
| `~ipo_name` | `Deepa Jewellers` | `name` |
| `Name` | `<a href="/gmp/deepa-jewellers-ipo/2081/">…</a><span class="badge">IPO</span>` | `detail_url`, exchange badge |
| `~IPO_Category` | `IPO` / `SME` | `ipo_type` |
| `~ipo_status1` | `U`/`O`/`C`/`LP`/`LN` | `source_status` (provenance only) |
| `GMP` | `₹<b>44</b> (24.86%)<br><small><b>44 ↓ / 55 ↑</b></small>` | `gmp`, `gmp_percentage`, `gmp_low`, `gmp_high` |
| `~gmp_percent_calc` | `24.86` | `gmp_percentage` (authoritative) |
| `Price (₹)` | `177` | `price_min`, `price_max` |
| `IPO Size` | `₹459.72 Cr` | `issue_size_crore` |
| `Lot` | `84` | `lot_size` |
| `Sub` | `0.88x` | `subscription_times` |
| `~Srt_Open` / `~Srt_Close` / `~Srt_BoA_Dt` / `~Str_Listing` | `2026-09-03` | the four dates, already ISO |
| `Rating` | `🔥🔥🔥🔥` | `rating` (glyph count) |
| `~P/E` | `12.13` | `pe_ratio` |
| `Anchor` | `✅` / `❌` | `has_anchor_investors` |

Anything not listed is preserved verbatim in the `ipos.raw_data` JSONB column.

---

## Pipeline

```
ScraperHTTPClient  →  RawPayload  →  ExtractorChain  →  ExtractionResult
                                          ↓
                                   IPONormalizer  →  NormalizedIPO[]
                                          ↓
                                    IPOValidator  →  ValidationReport
                                          ↓
                            confidence gate  →  IPORepository.upsert_many
```

Each stage lives in its own module and exchanges plain dataclasses
(`app/services/scraper/models.py`), so every stage is testable in isolation with
no database and no network.

| Module | Responsibility |
| --- | --- |
| `client.py` | HTTP fetch, retries, backoff, connection reuse, URL construction |
| `extractor.py` | Locate the dataset; identify its columns |
| `field_map.py` | Declarative canonical-field definitions — the resilience layer |
| `normalizer.py` | Raw cells → typed canonical values |
| `validator.py` | Per-record checks; run confidence score |
| `pipeline.py` | Orchestration and the persist/abort decision |

---

## Resilience: how columns are identified

The scraper never says "GMP is the third `<td>`" or
`soup.find("table", class_="ipo-table")`. Each canonical field declares what it
*looks like*, and columns are matched by meaning.

`field_map.py` defines each field with:

- **aliases** — header labels that mean it, covering the current names, the `~`
  machine names, and labels a human-facing table would plausibly use;
- **shape** — a predicate over values, used when the header is unrecognisable;
- **required** / **identity** flags that drive the confidence score.

Matching runs in descending order of trust:

| Strategy | Score | Example |
| --- | --- | --- |
| Exact label | 1.00 | `Close` → `close_date` |
| Token subset | 0.80 | `Close Date`, `IPO Close Dt` → `close_date` |
| Substring | 0.60 | `ipo category1` → `ipo_type` |
| **Value shape** | 0.45 | `zz_col_9` holding `2026-09-03`, `2026-09-04`… → a date field |

Machine (`~`) columns get a **+0.25 bonus**, so `~Srt_Close` (ISO dates) beats
the display `Close` (`3-Sep`) when both exist.

Assignment is greedy, best score first; each field and each column is used once,
so two similarly-named columns cannot collapse onto one field. Any column that
matches nothing is kept in `raw_data`.

### Dataset discovery

**JSON** — the row list is located *by shape*, not by key name: the largest list
of consistently-keyed dicts anywhere in the document wins. If `reportTableData`
were renamed tomorrow, extraction still succeeds. Per-cell `{"value": …}`
envelopes are unwrapped automatically.

**HTML** — three structural patterns are tried and scored, best wins:

1. real `<table>` elements, with or without `<thead>`;
2. ARIA grids (`role="table"`/`"row"`/`"cell"`) built from `<div>`s;
3. repeated `<dt>`/`<dd>` label-value blocks ("card" layouts).

Class names and ids are never used for selection. A table with fewer than two
columns is rejected — it is a notice or a placeholder, not a dataset.

---

## Confidence and validation

**Per record** (`IPOValidator`) — a row is rejected outright only for a *fatal*
problem: no identity, no name, `close_date` before `open_date` (which means the
columns were swapped), or a duplicate identity in the same batch.

Everything else is repaired rather than rejected, because one bad cell should
not cost an otherwise good IPO:

| Problem | Action |
| --- | --- |
| Value outside plausible bounds | Field cleared to `NULL`, warning recorded |
| Price band inverted (`min > max`) | Values swapped |
| Date more than 800 days from today | Field cleared (misparsed year) |
| Rating outside 1–5 | Cleared |

**Per run** — confidence blends two independent signals:

```
confidence = 0.6 × extraction_confidence   ("did we recognise the columns?")
           + 0.4 × validity_ratio          ("did the rows make sense?")
```

where `extraction_confidence = 0.7 × required_field_coverage + 0.3 × has_identity`.

They are averaged, not maximised: both must hold. Zero records scores `0.0`
regardless of how well the columns matched.

---

## Failure policy

When confidence falls below `SCRAPER_MIN_CONFIDENCE` (default `0.5`), or no
records were extracted, or none validated, the run:

1. **does not write anything** — existing IPO rows are left exactly as they were;
2. **retains the raw payload** in `scrape_raw_payloads` for replay;
3. **records the failure** in `scrape_runs` with a specific `error_code`:
   `SCRAPER_EXTRACTION_FAILED`, `SCRAPER_LOW_CONFIDENCE`,
   `SCRAPER_NO_VALID_RECORDS`, `SCRAPER_FETCH_FAILED`;
4. **logs** a structured `scrape.low_confidence_abort` / `scrape.failed` event.

The scraper never silently overwrites good data with a bad parse.

### Structure-change detection

Each run's field mapping is stored on `scrape_runs.field_mapping` and compared
against the last successful run. A field that used to map and no longer does —
or that moved to a different column — produces a warning like:

```
structure change: field 'gmp_percentage' (previously column '~gmp_percent_calc')
is no longer present
```

This surfaces an upstream redesign *before* it degrades into a confidence
failure. Check with:

```sql
SELECT started_at, status, confidence, warnings
FROM scrape_runs ORDER BY started_at DESC LIMIT 10;
```

### Source fallback

If the JSON feed is unreachable or its shape is unusable, the pipeline
automatically fetches `SCRAPER_PAGE_URL` and runs the HTML strategy over it,
keeping whichever attempt understood the data better. A warning records that the
fallback was used.

### Retry and rate limiting

- Retries: `SCRAPER_MAX_RETRIES` (default 3) with exponential backoff from
  `SCRAPER_RETRY_BACKOFF_SECONDS`.
- Retried only for transient conditions — timeouts, connection errors, `429`,
  `5xx`. A `404` is not retried, because retrying cannot help.
- Three scrapes a day is a very light load; the advisory lock guarantees no
  overlapping runs even with several worker replicas.
- A single pooled `httpx.AsyncClient` reuses connections across scrapes.

---

## Schedule

The scraper runs **three times a day**, each at a uniformly random moment inside
a half-hour window, in `APP_TIMEZONE`:

| Window | Fires somewhere in |
| --- | --- |
| `09:00` | 09:00:00 – 09:29:59 |
| `14:00` | 14:00:00 – 14:29:59 |
| `20:00` | 20:00:00 – 20:29:59 |

Configured as:

```env
SCRAPER_SCHEDULE_TIMES=09:00,14:00,20:00
SCRAPER_SCHEDULE_JITTER_MINUTES=30
```

The randomness is APScheduler's `jitter`, which adds `uniform(0, jitter)`
seconds to each fire time. It is **forward-only**, so a run never fires before
its window opens, and it is re-rolled on every firing — the time differs from
day to day rather than settling into a fixed offset.

Inspect the next fire times at any point:

```bash
make schedule        # or: docker compose exec worker python -m app.workers.run_once schedule
```

```
Scrape windows (random moment within each, re-rolled daily, +30m):
  09:00-09:30
  14:00-14:30
  20:00-20:30

Next fire times:
  scrape_ipos_0            2026-09-02T09:14:11+05:30
  scrape_ipos_1            2026-09-02T14:15:29+05:30
  scrape_ipos_2            2026-09-02T20:17:57+05:30
```

**Failure retry.** With only three runs a day, one failure would leave data
stale for hours, so a failed run schedules a single retry
`SCRAPER_FAILURE_RETRY_MINUTES` later (default 20; `0` disables). One retry
covers a transient upstream blip without hammering a genuinely broken source.

**First boot.** `SCRAPER_RUN_ON_STARTUP_IF_EMPTY` (default on) scrapes once at
start-up *only when the IPO table is empty*, so a fresh install has data
immediately without re-scraping on every restart.

**Interval mode.** Leaving `SCRAPER_SCHEDULE_TIMES` empty falls back to scraping
every `SCRAPER_INTERVAL_MINUTES`, which is handy in development.

---

## What each run does to the data

The source publishes only the **~30 currently-live IPOs**, so every run is an
upsert rather than a bulk import:

| Case | Action |
| --- | --- |
| IPO not seen before | Inserted |
| Seen before, a tracked value changed | Updated in place, snapshot appended |
| Seen before, nothing changed | Only `last_scraped_at` is touched |
| Previously seen, now absent from the feed | **Left alone** — it stays in the database |

Rows are matched on `(source, source_ipo_id)` using the upstream's own stable
id, so three runs a day produce three updates, never three copies. Because
departed IPOs are retained, the historical pool builds itself: the table holds
every IPO ever seen, while the feed only ever shows today's.

---

## Raw payload retention

`SCRAPER_RAW_RETENTION_MODE`:

| Value | Behaviour |
| --- | --- |
| `on_failure` (default) | Store only when a run fails — the case you need to debug |
| `always` | Store every response (~90 KB JSON / ~140 KB HTML per run) |
| `never` | Store nothing |

`SCRAPER_RAW_RETENTION_DAYS` (default 14) prunes older payloads after each run;
`0` disables pruning. Payloads live in their own table so the "how have recent
scrapes gone?" query never drags blobs through the buffer cache.

Retrieve one for debugging:

```sql
SELECT p.content
FROM scrape_raw_payloads p
JOIN scrape_runs r ON r.id = p.scrape_run_id
WHERE r.status = 'FAILED'
ORDER BY p.captured_at DESC LIMIT 1;
```

---

## Testing

All scraper tests use saved fixtures; **none contacts the live site**.

| Fixture | Purpose |
| --- | --- |
| `tests/fixtures/api/investorgain_report.json` | Real captured feed response |
| `tests/fixtures/html/investorgain_live_page.html` | The real (empty-table) live page |
| `variant_a_baseline.html` | Conventional server-rendered table |
| `variant_b_renamed_classes.html` | Every CSS class and id renamed |
| `variant_c_extra_wrappers.html` | Extra nested wrappers around and inside cells |
| `variant_d_reordered_columns.html` | Columns shuffled |
| `variant_e_missing_column.html` | GMP % column removed entirely |
| `variant_f_unrecognisable.html` | Structurally unrecognisable |

Variants A–E must all still extract; F must fail **safely** — zero records and
zero confidence, never a guess. Variant E is asserted against the baseline: the
same IPOs, same dates, same names, with only the removed field degraded (and
even then the percentage is recovered from the composite GMP cell).

Regenerate the variants after changing the generator:

```bash
make fixtures        # or: python scripts/generate_html_fixtures.py
```

---

## Adding support for a new upstream field

A new column is already captured in `raw_data` from the first scrape — nothing
is lost while you decide. To promote it to a real, queryable column:

1. **Declare it** in `CANONICAL_FIELDS` (`app/services/scraper/field_map.py`):

   ```python
   CanonicalField(
       name="sector",
       aliases=("sector", "industry", "sector name"),
       shape=_looks_like_name,
   )
   ```

2. **Add the column** to `IPO` (`app/db/models/ipo.py`) and, if it will be
   filtered or sorted, an index.

3. **Map it** in `NormalizedIPO` and `as_column_values()`
   (`app/services/scraper/models.py`), and set it in `IPONormalizer.normalize()`.

4. **Track changes** by adding it to `TRACKED_FIELDS` in
   `app/repositories/ipo.py` (and `SNAPSHOT_FIELDS` if it should be historised).

5. **Expose it** in `IPOResponse` and, if filterable, in `IPOFilterParams` plus
   one clause in `IPORepository._apply_filters`.

6. **Migrate**: `make migration m="add ipos.sector"`.

Steps 1 and 3 alone are enough to start collecting the value; the rest is only
needed to query it.

## If the upstream site changes

1. Look at recent runs: `SELECT started_at, status, confidence, error_code, warnings FROM scrape_runs ORDER BY started_at DESC LIMIT 20;`
2. Pull the retained payload for a failed run (query above) and save it as a new
   fixture under `tests/fixtures/`.
3. Write a failing test against that fixture.
4. Usually the fix is **adding an alias** in `field_map.py`, not changing parsing
   logic. If the delivery mechanism changed entirely, add a new
   `ExtractionStrategyBase` subclass and register it in `ExtractorChain`.
