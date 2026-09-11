# jobscraper

Polls **public ATS job-board APIs** and surfaces data engineering, data science,
AI/ML engineering and analytics roles — filtered by how recently they were
posted, how much experience they ask for, and whether they are in the US.

No browser, no proxies, no API keys. Every endpoint here is the one the
company's own careers page already calls.

**Current coverage:** 156 companies · ~42,000 postings tracked · ~2,500 matching
roles · 16 companies tagged with USCIS H-1B approval volume.

```
list call (1 req/board)  →  classify TITLES  →  detail fetch for matches only
                                   ↓
                          diff vs stored open set
                                   ↓
                      NEW / UPDATED / CLOSED events
```

---

## Contents

- [Quick start](#quick-start) · [Commands](#commands) · [How it works](#how-it-works)
- [Sources](#sources) · [Adding companies](#adding-companies)
- [Visa sponsorship](#visa-sponsorship--what-is-and-isnt-known)
- [Dashboard](#dashboard) · [Scheduling](#scheduling)
- [Project layout](#project-layout) · [Gotchas found the hard way](#gotchas-found-the-hard-way)

---

## Install

Python 3.11+. Three dependencies:

```bash
pip install -r requirements.txt
```

SQLite ships with Python. `jobs.db` is created on first run.

## Quick start

```bash
python -m jobscraper.cli import sources.yaml      # 129 verified boards
python -m jobscraper.cli poll --force             # first full crawl
python -m jobscraper.cli new --within 24h         # posted in the last day
python -m jobscraper.cli new --within 7d --max-exp 3   # last week, junior-friendly
```

## Commands

| Command | What it does |
|---|---|
| `import sources.yaml` | Bulk-add boards |
| `add <ats> <token> --company NAME` | Add one board |
| `discover <company.com>` | Sniff which ATS a careers page uses, print the `add` command |
| `check <ats> <token>` | Probe a board and preview matching roles — stores nothing |
| `poll [--force] [--dry-run] [--only ashby] [--company stripe]` | Fetch, diff, store |
| `new --within 24h --category data_engineer --remote` | Postings by date/role/location |
| `search "airflow" --seniority senior` | Keyword search over stored descriptions |
| `export --out jobs.csv --days 14` | CSV/JSON dump |
| `reclassify` | Re-score stored postings after editing the taxonomy |
| `enrich` | Fetch descriptions for matches that lack one |
| `workday-find <tenant>` | Resolve a Workday tenant to host + site |
| `stats` | Counts by category, seniority, recent events |
| `sources` | Board health: open matches, poll interval, last error |

Filters work on `new`, `search` and `export` alike. Categories: `data_engineer`,
`data_scientist`, `ai_engineer`, `analytics`.

### Date windows

Two different questions, two different flags:

- `--within 24h` — **the company posted it** within the window. This is the one
  you usually want. Accepts `24h`, `48h`, `7d`, `2w`, any `<number><h|d|w>`.
- `--seen-within 24h` — **this scraper first saw it** within the window. Useful
  once the database is warm, meaningless on a first crawl (everything is "new").

Postings with no date from the ATS are excluded by `--within`, since including
them would silently pad a "last 24 hours" answer with unknowns.

### Full-time only

`--full-time` drops interns, part-time, contract, temporary, seasonal and co-op
roles.

It uses two independent signals, because neither is reliable alone. Seniority is
matched on the title with word boundaries, so `International` and `Internal` are
never mistaken for `Intern`. `employment_type` is free text and arrives in at
least six spellings (`Full time`, `FullTime`, `Full Time`, `Full-time`,
`Permanent`, …) and is **null on ~43% of postings** — so nulls are kept. A
missing value is not evidence of a part-time role, and requiring an explicit
"full time" string would silently discard hundreds of real jobs.

### Experience

`--max-exp 3` keeps roles asking for at most 3 years. No Tier 1 ATS exposes
years-of-experience as a structured field, so this is read out of the
description text — measured at **75% coverage** across 949 live postings.

By default postings that state no requirement are **kept** (absence of a stated
minimum is not evidence of a high one). Add `--strict-exp` to drop them and see
only postings that explicitly ask for ≤ N years.

```bash
jobscraper new --within 7d --max-exp 3              # includes unstated
jobscraper new --within 7d --max-exp 3 --strict-exp # explicit only
```

## How it works

```
list call (1 req/board)  →  classify TITLES  →  detail fetch for matches only
                                   ↓
                          diff vs stored open set
                                   ↓
                      NEW / UPDATED / CLOSED events
```

**Title-first classification is the core optimization.** Greenhouse,
SmartRecruiters, Rippling and BambooHR omit descriptions from the list call.
Fetching detail for every posting would mean ~9,000 requests per crawl across
these 75 boards; filtering on the title first cuts that to ~940 — and only on
the first sighting, never again.

**Cheap re-polls.** Three short-circuits, in order:
1. `If-None-Match` — Greenhouse and Ashby both return ETags, so an unchanged
   board costs one 304.
2. List content hash — catches the rest.
3. Set diff — only genuinely new IDs trigger work.

**Adaptive cadence.** Interval grows ×1.5 per unchanged poll (cap 24 h), resets
to 15 min the moment anything changes. Quiet boards drift toward daily; active
ones stay fresh.

**The first crawl is the slow one — expect ~5-10 minutes for 75 boards.** Every
Greenhouse board shares `boards-api.greenhouse.io`, and the per-host cap of 2
concurrent requests deliberately serializes them. After that first pass, most
polls are a single 304 per board and finish in seconds.

**Shrink guard.** If a board returns 0 jobs, or under 50% of its previous count,
the run is marked `suspect` and the close pass is skipped — a truncated response
otherwise looks like every role being pulled at once.

## Sources

| ATS | Descriptions in list call? | Notes |
|---|---|---|
| **Ashby** | ✅ + structured comp | Richest source. ETag, `max-age=60`. |
| **Lever** | ✅ | `text` is the title; `createdAt` is epoch ms. |
| **Recruitee** | ✅ + salary min/max | Per-subdomain. |
| **Greenhouse** | ❌ | ETag. `content=true` works but is multi-MB (Stripe: 4.8 MB vs 389 KB). `per_page` is ignored. |
| **SmartRecruiters** | ❌ | Unknown company returns **200 + `totalFound: 0`**, never 404 — status code can't validate a token. Case-sensitive slugs. |
| **Rippling** | ❌ | Detail has `description.role`. |
| **BambooHR** | ❌ | Empty result for non-customers. |
| **Breezy** | ❌ (title-only) | `/json/{id}` serves HTML, not JSON, so no descriptions. |
| **Workable** | ⚠️ **unverified** | Both the v1 widget and v3 POST endpoints returned 200 with zero results for every account tried. Confirm against a live board before relying on it. |

### Tier 2: Workday

Added for enterprise coverage — most large H-1B sponsors live here. It behaves
differently from every Tier 1 board and costs meaningfully more:

- **POST**, not GET, with a JSON body.
- **`limit` hard-caps at 20** (50 and 100 both return HTTP 400), so NVIDIA's
  2,000 postings take 100 paged requests.
- **No ETag, `cache-control: no-store`** — there is no conditional-GET
  short-circuit. Every poll re-reads the whole board, which is why Workday
  sources default to a **6-hour** interval instead of 1 hour.
- The list call's `postedOn` is prose ("Posted 30+ Days Ago"); the real date is
  `startDate` in the detail payload, so `--within` needs the detail fetch.
- Facets exist but their IDs are tenant-specific hashes and the categories are
  far too coarse to narrow a crawl (NVIDIA: "Engineering" = 1,724 of 2,000).

**Finding a tenant.** Workday needs a host *and* a site name, not just a token.
The API distinguishes them for you: wrong host → **422**, right host with a
wrong site → **404**, both right → **200**. `workday-find` walks that:

```bash
jobscraper workday-find nvidia
# ✓ nvidia: nvidia.wd5.myworkdayjobs.com  site=NVIDIAExternalCareerSite  (2000 postings)
jobscraper workday-find nvidia --add
```

If it reports a host but no site, read the site from the company's careers URL
(`https://<host>/en-US/<SITE>`) and pass it directly:

```bash
jobscraper add workday cisco --company Cisco \
  --wd-host cisco.wd5.myworkdayjobs.com --wd-site Cisco_Careers
```

`discover <company.com>` also recognises Workday URLs on careers pages, though
JS-rendered career sites (NVIDIA's among them) hide the link — `workday-find`
is the reliable path.

## Visa sponsorship — what is and isn't known

The board list is **US tech companies on Tier 1 platforms**. Sponsorship history
is *not* verified per company, and the tool does not claim it is. Two things were
measured rather than assumed:

**Job text is not a usable sponsorship filter.** Across 949 postings, 92% say
nothing about sponsorship at all. Only 8% affirmatively mention it and 0.4%
explicitly rule it out. Filtering on description text would discard almost
everything for no reason.

**Large sponsors are mostly not on Tier 1.** Of 66 well-known high-volume H-1B
sponsors, only 5 have a reachable Greenhouse/Ashby/Lever board — Airbnb, Block,
LinkedIn, TCS and Disney — and only the first two list data roles. Amazon,
Google, Microsoft, Meta, Apple, JPMorgan, Capital One, Deloitte, Infosys and the
rest are on Workday, Oracle or Taleo, which are Tier 2.

So a Tier-1-only crawler structurally cannot cover the Fortune 500 sponsors.
Closing that gap means adding a Workday connector.

### H-1B approval counts

Boards carry an `h1b_approvals` field when the company appears in the **USCIS
top-100 sponsor list** (FY2020–24 approvals). Filter and sort by it:

```bash
jobscraper new --sponsor --full-time --within 7d       # top-100 sponsors only
jobscraper new --min-approvals 1000 --max-exp 3
```

**Absence of a count means "not in the top 100", never "does not sponsor."** Most
companies here sponsor and simply are not among the 100 largest filers.

### Why most top sponsors are unreachable

Of the 86 distinct employers in the top-100 list, only **12** have a reachable
Tier 1 or Workday board. The rest are dominated by IT staffing and consulting
firms — Cognizant, Infosys, HCL, Capgemini, Wipro, LTIMindtree, Tech Mahindra,
Mphasis, UST, Virtusa, Hexaware, Compunnel, Kforce — which run their own career
portals, plus Google, IBM, Deloitte, EY, PwC, Adobe and Intuit on custom sites or
Taleo/SuccessFactors. Four more were recovered by rendering their careers pages in a browser, because
the Workday link is injected by JavaScript and never appears in the raw HTML:

| Company | Host | Site |
|---|---|---|
| Adobe | `adobe.wd5` | `external_experienced` |
| HPE | `hpe.wd5` | `Jobsathpe` |
| Palo Alto Networks | `paloaltonetworks.wd5` | `panwexternalcareers` |
| Broadcom (ex-VMware) | `broadcom.wd1` | `External_Career` |

None of those site names were guessable from the tenant. VMware's own board is
retired — it returns HTTP 422, and its roles now live on Broadcom's board, which
is why Broadcom carries VMware's filing count.

Three remain unresolved with a known host but an unguessable site: **PwC,
Infosys and FIS**. `workday-find` prints the host, so the site can be read off
the company's careers URL (`https://<host>/en-US/<SITE>`) and added with
`add workday <tenant> --wd-host <host> --wd-site <SITE>`.

**Beware slug guessing.** Probing likely tokens produces convincing impostors:
`greenhouse/tcs` is a UK healthcare staffing firm, not Tata Consultancy;
`greenhouse/bcg` is a test board ("Voice AI Test"); `smartrecruiters/uber` holds a
single "Test UAT" posting; and the Google and EY Recruitee boards are demo
samples. Greenhouse, SmartRecruiters, Recruitee, Breezy and Workable all report
the company's own name in their payloads — check it before trusting a guessed
token. All 73 self-reporting boards in `sources.yaml` were audited this way and
matched.

## Adding companies

```bash
python -m jobscraper.cli discover stripe.com
python -m jobscraper.cli check ashby ramp        # preview matching roles first
python -m jobscraper.cli add ashby ramp --company Ramp
```

`discover` fetches `/`, `/careers` and `/jobs` and regexes for the ATS
fingerprint, so one call maps a company to its board.

## Title coverage

The taxonomy was tested against 82 real-world title variants across the four
families. Synonyms are handled, not just the canonical names — "Business
Intelligence Engineer", "BI Developer", "Dashboard Developer", "Decision Support
Analyst" and "Marketing Science Partner" all resolve to `analytics`; "DataOps
Engineer", "Data Modeler", "Software Engineer, Data" and "Streaming Engineer" all
resolve to `data_engineer`; "Member of Technical Staff, Machine Learning",
"Forward Deployed Engineer, AI" and "Perception Engineer" resolve to
`ai_engineer`.

After widening the taxonomy, re-score everything already stored without
re-crawling — titles are kept for every posting, matched or not:

```bash
jobscraper reclassify     # re-score stored titles
jobscraper enrich         # fetch descriptions for the newly matched
```

`enrich` exists because `poll` short-circuits on ETag/hash: a board that has not
changed is never re-read, so postings that only became matches after
`reclassify` would otherwise never get a description. `enrich` bypasses change
detection and fetches them directly.

Known deliberate exclusions: `Product Manager, Analytics` and `Program Manager,
Data Platform` are denied as management rather than data roles, and `Database
Developer` scores 0.8 (below threshold) as DBA-adjacent. Lower the bar with
`--threshold 0.5` on `poll`/`reclassify` if you disagree.

## Tuning the role filter

`jobscraper/classify.py` is the whole taxonomy — a deny list, then weighted
regex rules per category. Weight ≥ 1.0 matches on its own; lower weights need
corroboration from a matching department name (+0.3).

Deliberate calls you may want to flip:
- `Business Analyst` scores 0.5 (below threshold) — usually not a data role.
- `Financial Analyst` / `FP&A` are hard-denied as non-data analytics.
- `Data Center`, `Data Entry`, `Data Annotator` and recruiting roles are denied.

Lower the bar with `poll --threshold 0.5` to see borderline matches.

## Scheduling

**GitHub Actions owns the schedule.** `.github/workflows/refresh.yml` polls and
redeploys the dashboard without any local machine being awake:

| Tier | Cadence | Boards | Requests | Wall clock |
|---|---|---|---|---|
| Tier 1 | hourly | 131 | 131 (one per board, most return `304`) | ~30 s |
| Workday | 4x daily (02:15, 08:15, 14:15, 20:15 UTC) | 25 | ~1,400 (20 rows/request, no caching) | ~11 min |

Workday costs roughly 23x the requests of Tier 1 for a fifth of the boards,
which is why the two run separately. There is no `--force`, so each board's
adaptive interval still applies and an hourly tick rarely polls everything.

Run one by hand:

```bash
gh workflow run refresh.yml -f tier=tier1     # or workday, or all
```

`refresh.sh` still works locally for an ad-hoc poll against your own `jobs.db`:

```bash
./refresh.sh tier1
```

Both paths take the same `flock`, so concurrent runs serialise rather than
colliding on SQLite.

**Notes**

- GitHub disables scheduled workflows after 60 days without repository
  activity. A commit, or a manual run, resets that clock.
- If you ever schedule this locally instead, the project must live outside
  `~/Desktop`, `~/Documents` and `~/Downloads`. Those are TCC-protected on
  macOS and scheduled jobs are refused with `Operation not permitted` — for
  launchd as well as cron.

## Dashboard

`jobscraper dashboard` renders `dashboard.html` from the database: a filterable
desk of every matching role, with counts per filter and per-row triage
(shortlist / applied / dismissed).

Filters: posted window (24h / 48h / 7d / 30d), role family, max years of
experience (with a "only if stated" strictness toggle), seniority, US location,
full-time, remote, top-100 H-1B sponsors, free text, and six sort orders.

### Auto-refresh

The generated file is a self-contained snapshot — open it locally, or serve it.
Two ways to keep it current:

**Locally** — the installed crontab regenerates `dashboard.html` after every
poll. Only refreshes while the machine is awake.

**In CI** — `.github/workflows/refresh.yml` runs the poller on GitHub's
schedule and publishes the dashboard to GitHub Pages, so the page stays current
without any machine of yours being on. State carries between runs as a gzipped
SQLite file in the Actions cache (~15 MB after pruning non-matched payloads);
without it every run would look like a first crawl and `first_seen_at` would
reset, which is the whole basis of "new since last time". On a cold start the
workflow falls back to `seed/jobs.db.gz`, a committed snapshot of the posting
history. **The seed carries no description text** — this repository is public
and job descriptions are copyrighted — only the lifecycle dates and the fields
already derived from them.

Trigger a run by hand from the Actions tab, or:

```bash
gh workflow run refresh.yml -f tier=tier1
```

Note the cost shape on a **GitHub Free** account: Actions minutes are unlimited
for public repositories but capped at 2,000/month for private ones, and Pages
requires either a public repository or a paid plan. Hourly Tier 1 plus four
daily Workday runs lands around 3,000 minutes/month, so a private repo needs
either a slower cadence or a paid plan.

---

## Project layout

```
jobscraper/
  classify.py             role taxonomy: deny list + weighted regex per family
  location.py             free-text location -> US / non-US / unknown
  experience.py           "5+ years" extraction from description prose
  connectors.py           one class per ATS; Tier 1 + Workday
  pipeline.py             fetch, paginate, diff, enrich, persist
  db.py                   SQLite schema, migrations, blocklist helpers
  lock.py                 flock so overlapping scheduled polls serialise
  dashboard.py            renders dashboard.html from the database
  dashboard_template.html the dashboard itself (filters, triage, theming)
  cli.py                  command line interface
sources.yaml              verified boards, with H-1B approval counts
blocklist.yaml            companies never to track (IT services, staffing)
refresh.sh                scheduled entry point: poll + regenerate dashboard
```

Data model: `source` (one row per company board) → `job` (one row per posting,
with `first_seen_at` / `closed_at` lifecycle) → `event` (new/updated/closed) and
`poll_run` (per-poll audit). Descriptions and raw payloads are stored **only**
for postings that match the role filter; every posting gets a lightweight row so
close-detection stays accurate.

---

## Gotchas found the hard way

Each of these was a real bug, found by testing against live boards:

| What looked fine | What was actually happening |
|---|---|
| Workday pagination | `total` is a **cap, not a count**. Three tenants report exactly `2000` while still serving records at offset 2400 — and one served only 727 unique rows, repeating the rest. Pagination now dedupes and stops on a short, empty or repeated page. |
| Workday page 2+ | Only the **first** page carries `total`; later pages report `total: 0`. Reading it from the latest page capped every board at 40 rows. |
| `greenhouse/tcs` | A **UK healthcare staffing firm**, not Tata Consultancy. Guessed slugs produce convincing impostors; `greenhouse/bcg` is a test board. Greenhouse, SmartRecruiters, Recruitee, Breezy and Workable all self-report a company name — check it. |
| "india" in a location | Matched inside **INDIANAPOLIS**. Country patterns need word boundaries. |
| US state list | **Georgia and Hawaii were missing**, silently dropping every posting in those two states. |
| "Gemini" sponsor match | Matched **Capgemini** by substring, inheriting its 9,348 filings. Identity matching must be whole-word. |
| Suffix stripping | A blind `replace("the", "")` turned **The**rmo Fisher into "rmofisher". |
| `--days 7` | Filtered on when the *scraper* found a job, not when it was *posted* — so everything looked new on a first crawl. |
| Applied Scientist II | Read as *junior*: the roman numeral hit the `jr` rule. |
| Scheduled jobs on macOS | `~/Desktop` is TCC-protected. Neither cron nor launchd can read it — the project has to live outside Desktop/Documents/Downloads. |

---

## Legal and etiquette

- Tier 1 and Workday endpoints are **public, unauthenticated APIs that the
  companies' own career pages call**. Fetching them is ordinary HTTP, but the
  ATS vendors' terms still apply to their hosted pages — read Greenhouse's and
  Ashby's before redistributing commercially.
- Job descriptions are copyrighted. Fine for personal search and local
  indexing; store a snippet and a link if you republish.
- The poller caps at 8 concurrent requests globally and 2 per host, leaves
  ~0.5 s between calls to the same host, honours `Retry-After`, and disables a
  source after 10 consecutive failures. **Put a real contact address in `UA`**
  (`jobscraper/pipeline.py`) before running this on a schedule.
