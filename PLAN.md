# Job Board Scraper — Implementation Plan

## 0. The core insight

You almost never need a browser. ~80% of tech job postings live on ~10 ATS
platforms, and most of them expose an **unauthenticated JSON endpoint** that the
company's own careers page calls. You fetch the full job list for a company in
one request, diff it against last run, and only fetch detail pages for IDs you
haven't seen.

Scrape HTML only as a last resort (iCIMS, SuccessFactors, bespoke career pages).

---

## 1. Source tiers

### Tier 1 — public JSON list API, no auth, whole board in 1–2 calls
These are the backbone. All verified live on 2026-09-10.

| ATS | Endpoint | Notes |
|---|---|---|
| **Greenhouse** | `GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` | 200, ETag. `content=true` returns full HTML descriptions — Stripe's board is 4.8 MB. `per_page` is **ignored**; you always get everything. Omit `content` for a cheap 389 KB id/title/location list. Detail: `/jobs/{id}`. Also `/departments`, `/offices`. Invalid token → 404. |
| **Ashby** | `GET https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=true` | 200, ETag + `max-age=60`. Officially documented. Returns descriptions, `secondaryLocations`, and structured comp bands. Ramp = 2.6 MB. |
| **Lever** | `GET https://api.lever.co/v0/postings/{company}?mode=json` | 200. Supports `limit`, `skip`, `location=`, `team=`, `commitment=`. Descriptions inline as both HTML and `additionalPlain`. Invalid slug → 404 `{"ok":false}`. |
| **SmartRecruiters** | `GET https://api.smartrecruiters.com/v1/companies/{id}/postings?limit=100&offset=0` | 200. **Gotcha:** unknown company returns `200 {"totalFound":0}`, not 404 — you cannot use status code to validate slugs. Slug is case-sensitive (`SmartRecruiters` works, `Ubisoft` returns 0). Detail: `/postings/{id}`. |
| **Rippling ATS** | `GET https://api.rippling.com/platform/api/ats/v1/board/{company}/jobs` | 200, flat array. Detail: `.../jobs/{uuid}`. |
| **Recruitee** | `GET https://{company}.recruitee.com/api/offers/` | 200, full descriptions + requirements inline. |
| **Breezy HR** | `GET https://{company}.breezy.hr/json` | 200, flat array with `published_date`. |
| **BambooHR** | `GET https://{company}.bamboohr.com/careers/list` → `/careers/{id}/detail` | 200. Empty result for non-customers. |
| **Workable** | `GET https://apply.workable.com/api/v1/widget/accounts/{account}?details=true` | 200. Newer boards use `POST https://apply.workable.com/api/v3/accounts/{account}/jobs` with `{"query":"","location":[]}`. |
| **Pinpoint** | `GET https://{company}.pinpointhq.com/postings.json` | JSON list. |
| **Comeet** | `GET https://www.comeet.co/careers-api/2.0/company/{uid}/positions?token={tok}` | uid+token are public, embedded in the careers page HTML. |
| **Personio** | `GET https://{company}.jobs.personio.de/xml` | XML feed (307-redirects; follow with `-L`). |

### Tier 2 — public but awkward (POST bodies, pagination, tenant discovery)

| ATS | How |
|---|---|
| **Workday** (biggest enterprise share) | `POST https://{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs` with `{"appliedFacets":{},"limit":20,"offset":0,"searchText":""}`. Verified on NVIDIA: returns `{"total":2000, "jobPostings":[...]}`. `limit` caps at 20. Detail: `GET /wday/cxs/{tenant}/{site}/job/{externalPath}`. Pain: you must discover `wd1/wd3/wd5…` and the site name per company — scrape them once from the public careers URL. |
| **Oracle Recruiting Cloud (ORC)** | `GET https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true&expand=requisitionList.secondaryLocations&finder=findReqs;siteNumber=CX_1,limit=200,offset=0`. Verified 200. Host varies per tenant (`*.fa.*.oraclecloud.com`). |
| **Jobvite** | `https://jobs.jobvite.com/{company}/jobs` HTML, or the XML feed where enabled. |
| **JazzHR** | `https://{company}.applytojob.com/apply/jobs/rss` (RSS, cheap and stable). |
| **Teamtailor** | `https://{company}.teamtailor.com/jobs.rss`, or REST API with a per-company key. |
| **Zoho Recruit / Paylocity / Paycom / UKG / ADP** | Undocumented internal JSON. Find via DevTools → Network → XHR once per vendor, then it's stable for months. |

### Tier 3 — HTML / headless only
iCIMS (`careers-{co}.icims.com/jobs/search?pr=0&in_iframe=1`), SAP SuccessFactors,
Taleo classic, and bespoke Next.js career pages. Strategy: check for
`<script type="application/ld+json">` with `"@type":"JobPosting"` first — Google
for Jobs SEO means a surprising number of custom pages hand you clean structured
data. Only reach for Playwright if that fails.

### Tier 4 — aggregators (breadth, lower quality, use as discovery not truth)
- **Himalayas** `https://himalayas.app/jobs/api?limit=…&cursor=…` — 200, cursor pagination, remote-focused. Good free firehose.
- **Remotive** `https://remotive.com/api/remote-jobs` — 200, ~200 KB, asks for ≤1 call per day.
- **RemoteOK** `https://remoteok.com/api` — 200; ToS requires an attribution backlink.
- **HN "Who is Hiring"** via Algolia: `https://hn.algolia.com/api/v1/search_by_date?tags=comment,story_{id}&hitsPerPage=1000`.
- **USAJOBS** `https://data.usajobs.gov/api/search` — needs a free API key (`Authorization-Key` + `User-Agent` headers); returns 401 without.
- **Adzuna / Jooble / Careerjet** — free-tier keyed APIs, decent for non-tech and non-US coverage.
- **Avoid**: Indeed (publisher API shut down, aggressive anti-bot), LinkedIn (ToS + Cloudflare), Glassdoor. Not worth the maintenance tax.

---

## 2. Canonical schema

Normalize everything into one shape. Keep the raw payload forever — re-parsing
history is free, re-fetching it is not.

```sql
CREATE TABLE company (
  id            BIGSERIAL PRIMARY KEY,
  name          TEXT NOT NULL,
  domain        TEXT UNIQUE,
  careers_url   TEXT
);

CREATE TABLE source (              -- one row per company-board
  id            BIGSERIAL PRIMARY KEY,
  company_id    BIGINT REFERENCES company(id),
  ats           TEXT NOT NULL,     -- 'greenhouse' | 'ashby' | 'lever' | ...
  board_token   TEXT NOT NULL,     -- slug / tenant / uid
  config        JSONB,             -- workday host+site, comeet token, etc.
  enabled       BOOL DEFAULT TRUE,
  poll_interval_s INT DEFAULT 3600,
  last_polled_at  TIMESTAMPTZ,
  last_etag       TEXT,
  last_hash       TEXT,            -- hash of normalized list
  consecutive_failures INT DEFAULT 0,
  UNIQUE (ats, board_token)
);

CREATE TABLE job (
  id            BIGSERIAL PRIMARY KEY,
  source_id     BIGINT REFERENCES source(id),
  external_id   TEXT NOT NULL,     -- ATS-native id
  url           TEXT NOT NULL,
  title         TEXT NOT NULL,
  department    TEXT,
  team          TEXT,
  locations     TEXT[],
  is_remote     BOOL,
  employment_type TEXT,
  salary_min    NUMERIC, salary_max NUMERIC, salary_currency TEXT,
  description_html TEXT,
  description_text TEXT,
  posted_at     TIMESTAMPTZ,
  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  closed_at     TIMESTAMPTZ,       -- set when it disappears from the board
  content_hash  TEXT,
  raw           JSONB NOT NULL,
  UNIQUE (source_id, external_id)
);
CREATE INDEX ON job (source_id) WHERE closed_at IS NULL;
CREATE INDEX ON job USING GIN (to_tsvector('english', title || ' ' || coalesce(description_text,'')));
```

`first_seen_at` / `closed_at` are the whole product if you care about *new*
postings — ATS `posted_at` fields lie constantly (companies re-post, bulk-edit,
or leave it null).

---

## 3. Architecture

```
┌───────────────┐   ┌──────────────┐   ┌──────────────┐   ┌───────────┐
│ Board registry│──▶│  Scheduler   │──▶│  Connectors  │──▶│ Raw store │
│ (source table)│   │ (due sources)│   │ (1 per ATS)  │   │ (S3/JSONB)│
└───────────────┘   └──────────────┘   └──────┬───────┘   └─────┬─────┘
                                              ▼                 ▼
                                       ┌─────────────┐   ┌─────────────┐
                                       │ Normalizer  │──▶│  Postgres   │
                                       └─────────────┘   └──────┬──────┘
                                              │                 ▼
                                              ▼          ┌─────────────┐
                                       ┌─────────────┐   │  Diff engine│
                                       │  Enrichment │◀──│ NEW/UPD/DEL │
                                       │ salary,skill│   └──────┬──────┘
                                       │ remote, embd│          ▼
                                       └─────────────┘   ┌─────────────┐
                                                         │Alerts/API/UI│
                                                         └─────────────┘
```

**Connector interface** — the only thing every ATS must implement:

```python
class Connector(Protocol):
    ats: str
    async def list_jobs(self, src: Source) -> list[RawJob]: ...
    async def fetch_detail(self, src: Source, job: RawJob) -> RawJob: ...  # optional
    def normalize(self, raw: dict, src: Source) -> Job: ...
    @staticmethod
    def validate_token(token: str) -> bool: ...   # cheap probe for discovery
```

Greenhouse/Ashby/Lever/Recruitee need no `fetch_detail` at all — the list call
already carries descriptions. That's why they're Tier 1.

---

## 4. Polling & change detection

The whole loop, per source:

1. **Conditional GET.** Both Greenhouse and Ashby return `ETag` (verified).
   Send `If-None-Match: {last_etag}`. A `304` costs you nothing — stop here.
2. **Content hash.** For sources without ETags, hash the sorted list of
   `(external_id, title, location)` tuples. Unchanged hash → stop.
3. **Set diff.** `current_ids` vs `open_ids_in_db`:
   - `current - db` → **NEW** → fetch detail if needed, insert, emit event.
   - `db ∩ current` → update `last_seen_at`; if per-job `content_hash` changed,
     emit **UPDATED** (title/comp/location changes are the interesting ones).
   - `db - current` → **CLOSED**. Guard against a truncated response: if the
     board returns 0 jobs or <50% of last count, mark the run suspect and skip
     the close pass rather than mass-closing a live board.
4. **Adaptive cadence.** Start every source at 1 h. Multiply the interval by 1.5
   (cap 24 h) after each unchanged poll; reset to 15 min on any change. High-churn
   boards get polled often, dead boards drift to daily — 10× fewer requests for
   the same freshness.
5. **Failure backoff.** Exponential with jitter; disable after ~10 consecutive
   failures and flag for manual review (usually means the company migrated ATS).

**Concurrency & politeness**: global semaphore ~10, per-host semaphore 2, 1–2 s
gap between calls to the same host, `User-Agent` naming your bot with a contact
URL, respect `Retry-After` on 429. These APIs are public and cheap — you'll never
need proxies if you behave. Note Greenhouse `content=true` is multi-MB per board:
prefer the light list call and fetch detail only for new IDs when you're tracking
thousands of boards.

---

## 5. The hard part: discovering board tokens

Connectors are easy; knowing that Ramp is `ashby/ramp` is the real work.

1. **Seed manually** — hand-curate 200–500 target companies. Highest signal per
   hour of effort, and honestly enough for a personal job tracker.
2. **Detect from a careers page** — fetch `https://{domain}/careers` and regex for
   `boards.greenhouse.io/(\w+)`, `job-boards.greenhouse.io/(\w+)`,
   `jobs.lever.co/([\w-]+)`, `jobs.ashbyhq.com/([\w-]+)`,
   `([\w-]+)\.myworkdayjobs\.com`, `([\w-]+)\.recruitee\.com`, etc. Also check the
   `<iframe>`/`<script>` srcs and any `fetch()` URLs in the page bundle. One HTTP
   call maps a company to its ATS.
3. **Bulk mining** — query Common Crawl's URL index for
   `boards.greenhouse.io/*` / `jobs.ashbyhq.com/*` / `jobs.lever.co/*`; each
   distinct first path segment is a candidate token. Validate with a cheap HEAD.
4. **Aggregator backfill** — Himalayas/Remotive listings link straight to
   `jobs.ashbyhq.com/...` URLs. Mine the outbound links to discover tokens, then
   drop the aggregator and poll the ATS directly (fresher, richer, no ToS issue).
5. **Certificate Transparency** — crt.sh for `careers.*` / `jobs.*` subdomains
   of your target domains, then resolve the CNAME to identify the ATS.

Re-validate tokens weekly; companies migrate ATS more often than you'd think.

---

## 6. Enrichment (after the pipeline is boring)

- HTML → text (`selectolax` or `trafilatura`), strip boilerplate EEO blocks.
- **Salary**: use structured fields where offered (Ashby `compensation`,
  SmartRecruiters, Greenhouse pay ranges); regex fallback for `$150,000 - $190,000`
  and `$75/hr` patterns in the body, normalized to annual USD.
- **Remote/hybrid/onsite** classification from location strings + description.
- **Seniority** from title (intern/new grad/junior/senior/staff/principal).
- **Skills**: keyword dictionary first; an LLM pass only on jobs that survive
  your filters — it's the expensive step, so put it last in the funnel.
- **Visa sponsorship** signals ("will not sponsor", "US citizenship required").
- **Embeddings** (pgvector) if you want semantic "jobs like this one" search.

---

## 7. Phased roadmap

**Phase 1 — one connector, end to end (½ day)**
Greenhouse only. `httpx` + Postgres + a `poll()` function + the diff logic.
Ship a CLI: `jobscraper add greenhouse stripe` / `jobscraper poll --all`.

**Phase 2 — the Tier 1 set (1–2 days)**
Add Ashby, Lever, SmartRecruiters, Recruitee, Workable, Rippling, Breezy.
They're each 30–60 lines once the interface exists. Add ETag/hash short-circuit,
adaptive cadence, retries.

**Phase 3 — discovery (1 day)**
Careers-page ATS sniffer + a `sources.yaml` you can hand-edit. Get to a few
hundred boards.

**Phase 4 — Workday + Oracle (1 day)**
Worth it purely for enterprise coverage; both are POST/param quirks, not scraping.

**Phase 5 — serving (1–2 days)**
FastAPI read API, full-text + filter search, and a NEW-jobs alert
(email/Slack/RSS) driven off `first_seen_at`.

**Phase 6 — enrichment & HTML fallbacks**
Salary/remote/seniority parsing, then Playwright for the handful of boards that
justify it.

**Stack**: Python 3.12, `httpx` + `asyncio`, Pydantic models, Postgres (JSONB +
`tsvector` + optional pgvector), APScheduler or a plain cron loop to start —
resist Airflow/Temporal until you actually have DAG-shaped problems. Deploy as
one container + Postgres; this fits comfortably on a $10 VPS at a few thousand
boards.

---

## 8. Legal & operational notes

- Tier 1/2 endpoints are **public, unauthenticated APIs that the companies'
  own public career pages call**. Fetching them is ordinary HTTP, but the ATS
  vendors' ToS still apply to their hosted board pages — read Greenhouse's and
  Ashby's if you plan to redistribute commercially.
- Respect `robots.txt` for anything HTML you scrape, identify your bot, and keep
  request rates low enough that no one notices you.
- Job descriptions are copyrighted text. Safe for personal use and for search
  indexing; if you republish, store a snippet + link, not the full body.
- Aggregators with explicit ToS (RemoteOK's backlink requirement, Remotive's
  1-call-per-day) — honor them or skip those sources.
- Don't touch LinkedIn/Indeed programmatically. The maintenance and legal cost
  dwarfs the incremental coverage.
