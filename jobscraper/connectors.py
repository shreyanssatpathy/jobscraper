"""Tier 1 connectors: public, unauthenticated JSON board APIs.

Each connector turns one company's board into a list of RawJob. Some ATSes ship
descriptions in the list call (Ashby, Lever, Recruitee) and need no detail fetch
at all; the rest expose a per-job endpoint that we only call for postings that
already passed the role filter.

All endpoint shapes here were verified against live boards on 2026-09-10.
"""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\xa0]+")
_NL = re.compile(r"\n{3,}")


def html_to_text(raw: str | None) -> str | None:
    """Cheap, dependency-free HTML -> text. Good enough for keyword matching."""
    if not raw:
        return None
    s = html.unescape(raw)
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h[1-6]>", "\n", s)
    s = re.sub(r"(?i)<li[^>]*>", "- ", s)
    s = _TAG.sub("", s)
    s = html.unescape(s)
    s = _WS.sub(" ", s)
    s = _NL.sub("\n\n", s)
    return s.strip() or None


def _iso(value: Any) -> str | None:
    """Normalize the assorted timestamp formats these APIs emit to ISO-8601."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):          # Lever: epoch milliseconds
        secs = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(secs, tz=timezone.utc).isoformat(timespec="seconds")
    s = str(value).strip().replace("Z", "+00:00")
    s = re.sub(r" UTC$", "+00:00", s)            # Recruitee: "2026-09-08 14:55:36 UTC"
    try:
        return datetime.fromisoformat(s).astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return str(value)


_MONEY = re.compile(
    r"\$\s*([\d,]+(?:\.\d+)?)\s*([kK])?\s*(?:-|–|—|to)\s*\$?\s*([\d,]+(?:\.\d+)?)\s*([kK])?")


def parse_salary(text: str | None) -> tuple[float | None, float | None, str | None]:
    """Pull a min/max out of free-text pay strings like '$211.4K - $290.6K'."""
    if not text:
        return None, None, None
    m = _MONEY.search(text)
    if not m:
        return None, None, None
    lo = float(m.group(1).replace(",", "")) * (1000 if m.group(2) else 1)
    hi = float(m.group(3).replace(",", "")) * (1000 if m.group(4) else 1)
    return lo, hi, "USD"


@dataclass
class RawJob:
    external_id: str
    title: str
    url: str | None = None
    department: str | None = None
    team: str | None = None
    location: str | None = None
    is_remote: bool | None = None
    employment_type: str | None = None
    posted_at: str | None = None
    description_text: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    salary_raw: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class Req:
    url: str
    method: str = "GET"
    json_body: dict | None = None


class Connector:
    ats: str = ""
    #: True when the list call omits descriptions and a per-job fetch is needed.
    needs_detail: bool = False

    def list_request(self, token: str, config: dict) -> Req:
        raise NotImplementedError

    def parse_list(self, data: Any, token: str, config: dict) -> list[RawJob]:
        raise NotImplementedError

    #: Rows a full page holds, for paged boards. The pipeline uses it to stop on
    #: a short page; None means the board is never paged.
    page_size: int | None = None

    def next_request(self, data: Any, token: str, config: dict,
                     fetched: int) -> Req | None:
        """Next page, or None when the board is exhausted.

        `data` is always the FIRST page's payload, because that is the only one
        Workday puts a row count in. Only Workday needs this -- every Tier 1
        board returns its whole list in a single response.
        """
        return None

    def detail_request(self, job: RawJob, token: str, config: dict) -> Req | None:
        return None

    def apply_detail(self, job: RawJob, data: Any) -> None:
        """Enrich `job` in place from its detail payload."""

    def board_url(self, token: str, config: dict) -> str:
        return ""


# --------------------------------------------------------------------------
# Greenhouse -- boards-api.greenhouse.io
# List omits descriptions unless content=true, which returns multi-MB payloads
# (Stripe: 4.8 MB vs 389 KB). We take the light list and fetch detail per match.
# --------------------------------------------------------------------------
class Greenhouse(Connector):
    ats = "greenhouse"
    needs_detail = True

    def list_request(self, token, config):
        return Req(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs")

    def parse_list(self, data, token, config):
        out = []
        for j in data.get("jobs", []):
            out.append(RawJob(
                external_id=str(j["id"]),
                title=(j.get("title") or "").strip(),
                url=j.get("absolute_url"),
                department=(j.get("departments") or [{}])[0].get("name") if j.get("departments") else None,
                location=(j.get("location") or {}).get("name"),
                posted_at=_iso(j.get("first_published") or j.get("updated_at")),
                raw=j,
            ))
        return out

    def detail_request(self, job, token, config):
        return Req(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job.external_id}")

    def apply_detail(self, job, data):
        job.description_text = html_to_text(data.get("content"))
        job.department = job.department or (data.get("departments") or [{}])[0].get("name")
        pay = (data.get("pay_input_ranges") or [{}])[0]
        if pay.get("min_cents"):
            job.salary_min = pay["min_cents"] / 100
            job.salary_max = (pay.get("max_cents") or 0) / 100 or None
            job.salary_currency = pay.get("currency_type")
        job.raw = data

    def board_url(self, token, config):
        return f"https://job-boards.greenhouse.io/{token}"


# --------------------------------------------------------------------------
# Ashby -- richest Tier 1 source: descriptions + structured comp in one call.
# --------------------------------------------------------------------------
class Ashby(Connector):
    ats = "ashby"

    def list_request(self, token, config):
        return Req(f"https://api.ashbyhq.com/posting-api/job-board/{token}"
                   f"?includeCompensation=true")

    def parse_list(self, data, token, config):
        out = []
        for j in data.get("jobs", []):
            if j.get("isListed") is False:
                continue
            comp = j.get("compensation") or {}
            summary = (comp.get("scrapeableCompensationSalarySummary")
                       or comp.get("compensationTierSummary"))
            lo, hi, cur = parse_salary(summary)
            secondary = [s.get("location") for s in (j.get("secondaryLocations") or [])]
            loc = ", ".join(x for x in [j.get("location"), *secondary] if x)
            out.append(RawJob(
                external_id=j["id"],
                title=(j.get("title") or "").strip(),   # Ashby titles can carry leading spaces
                url=j.get("jobUrl"),
                department=j.get("department"),
                team=j.get("team"),
                location=loc or None,
                is_remote=j.get("isRemote"),
                employment_type=j.get("employmentType"),
                posted_at=_iso(j.get("publishedAt")),
                description_text=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")),
                salary_min=lo, salary_max=hi, salary_currency=cur, salary_raw=summary,
                raw=j,
            ))
        return out

    def board_url(self, token, config):
        return f"https://jobs.ashbyhq.com/{token}"


# --------------------------------------------------------------------------
# Lever
# --------------------------------------------------------------------------
class Lever(Connector):
    ats = "lever"

    def list_request(self, token, config):
        return Req(f"https://api.lever.co/v0/postings/{token}?mode=json")

    def parse_list(self, data, token, config):
        out = []
        for j in data:
            cats = j.get("categories") or {}
            body = "\n\n".join(x for x in (j.get("descriptionPlain"),
                                           j.get("additionalPlain")) if x)
            lo, hi, cur = parse_salary(j.get("salaryDescriptionPlain"))
            rng = j.get("salaryRange") or {}
            if rng.get("min"):
                lo, hi, cur = rng.get("min"), rng.get("max"), rng.get("currency")
            locs = cats.get("allLocations") or ([cats["location"]] if cats.get("location") else [])
            out.append(RawJob(
                external_id=j["id"],
                title=(j.get("text") or "").strip(),
                url=j.get("hostedUrl"),
                department=cats.get("department") or cats.get("team"),
                team=cats.get("team"),
                location=", ".join(locs) or None,
                is_remote=(j.get("workplaceType") == "remote") if j.get("workplaceType") else None,
                employment_type=cats.get("commitment"),
                posted_at=_iso(j.get("createdAt")),
                description_text=body or None,
                salary_min=lo, salary_max=hi, salary_currency=cur,
                raw=j,
            ))
        return out

    def board_url(self, token, config):
        return f"https://jobs.lever.co/{token}"


# --------------------------------------------------------------------------
# SmartRecruiters
# Gotcha: an unknown company returns 200 with totalFound=0, never 404, so the
# status code cannot be used to validate a token. Slugs are case-sensitive.
# --------------------------------------------------------------------------
class SmartRecruiters(Connector):
    ats = "smartrecruiters"
    needs_detail = True
    PAGE = 100

    def list_request(self, token, config):
        return Req(f"https://api.smartrecruiters.com/v1/companies/{token}"
                   f"/postings?limit={self.PAGE}&offset=0")

    def parse_list(self, data, token, config):
        out = []
        for j in data.get("content", []):
            loc = j.get("location") or {}
            parts = [loc.get("city"), loc.get("region"), loc.get("country")]
            out.append(RawJob(
                external_id=str(j["id"]),
                title=(j.get("name") or "").strip(),
                url=f"https://jobs.smartrecruiters.com/{token}/{j['id']}",
                department=(j.get("department") or {}).get("label") or (j.get("function") or {}).get("label"),
                location=", ".join(x for x in parts if x) or None,
                is_remote=loc.get("remote"),
                employment_type=(j.get("typeOfEmployment") or {}).get("label"),
                posted_at=_iso(j.get("releasedDate")),
                raw=j,
            ))
        return out

    def detail_request(self, job, token, config):
        return Req(f"https://api.smartrecruiters.com/v1/companies/{token}/postings/{job.external_id}")

    def apply_detail(self, job, data):
        sections = (data.get("jobAd") or {}).get("sections") or {}
        body = "\n\n".join(
            html_to_text((sections.get(k) or {}).get("text")) or ""
            for k in ("jobDescription", "qualifications", "additionalInformation"))
        job.description_text = body.strip() or None
        job.url = data.get("postingUrl") or job.url
        job.raw = data

    def board_url(self, token, config):
        return f"https://careers.smartrecruiters.com/{token}"


# --------------------------------------------------------------------------
# Recruitee -- full descriptions and structured salary in the list call.
# --------------------------------------------------------------------------
class Recruitee(Connector):
    ats = "recruitee"

    def list_request(self, token, config):
        return Req(f"https://{token}.recruitee.com/api/offers/")

    def parse_list(self, data, token, config):
        out = []
        for j in data.get("offers", []):
            if j.get("status") and j["status"] != "published":
                continue
            sal = j.get("salary") or {}
            out.append(RawJob(
                external_id=str(j["id"]),
                title=(j.get("title") or "").strip(),
                url=j.get("careers_url"),
                department=j.get("department"),
                location=j.get("location"),
                is_remote=bool(j.get("remote")),
                employment_type=j.get("employment_type_code"),
                posted_at=_iso(j.get("published_at") or j.get("created_at")),
                description_text=html_to_text(j.get("description")),
                salary_min=float(sal["min"]) if sal.get("min") else None,
                salary_max=float(sal["max"]) if sal.get("max") else None,
                salary_currency=sal.get("currency"),
                raw=j,
            ))
        return out

    def board_url(self, token, config):
        return f"https://{token}.recruitee.com/"


# --------------------------------------------------------------------------
# Rippling ATS
# --------------------------------------------------------------------------
class Rippling(Connector):
    ats = "rippling"
    needs_detail = True

    def list_request(self, token, config):
        return Req(f"https://api.rippling.com/platform/api/ats/v1/board/{token}/jobs")

    def parse_list(self, data, token, config):
        out = []
        for j in data:
            out.append(RawJob(
                external_id=j["uuid"],
                title=(j.get("name") or "").strip(),
                url=j.get("url"),
                department=(j.get("department") or {}).get("label"),
                location=(j.get("workLocation") or {}).get("label"),
                raw=j,
            ))
        return out

    def detail_request(self, job, token, config):
        return Req(f"https://api.rippling.com/platform/api/ats/v1/board/{token}/jobs/{job.external_id}")

    def apply_detail(self, job, data):
        desc = data.get("description") or {}
        job.description_text = html_to_text(desc.get("role")) or html_to_text(desc.get("company"))
        job.employment_type = data.get("employmentType")
        job.posted_at = _iso(data.get("createdOn"))
        pay = data.get("payRangeDetails") or {}
        if isinstance(pay, dict) and pay.get("min"):
            job.salary_min, job.salary_max = pay.get("min"), pay.get("max")
            job.salary_currency = pay.get("currency")
        job.raw = data

    def board_url(self, token, config):
        return f"https://ats.rippling.com/{token}/jobs"


# --------------------------------------------------------------------------
# Breezy HR -- list only. The /json/{id} detail route serves HTML, not JSON,
# so these stay title-level; classification still works fine off the title.
# --------------------------------------------------------------------------
class Breezy(Connector):
    ats = "breezy"

    def list_request(self, token, config):
        return Req(f"https://{token}.breezy.hr/json")

    def parse_list(self, data, token, config):
        out = []
        for j in data:
            loc = j.get("location") or {}
            lo, hi, cur = parse_salary(j.get("salary"))
            out.append(RawJob(
                external_id=str(j["id"]),
                title=(j.get("name") or "").strip(),
                url=j.get("url"),
                department=j.get("department"),
                location=loc.get("name"),
                is_remote=loc.get("is_remote"),
                employment_type=(j.get("type") or {}).get("name"),
                posted_at=_iso(j.get("published_date")),
                salary_min=lo, salary_max=hi, salary_currency=cur,
                salary_raw=j.get("salary"),
                raw=j,
            ))
        return out

    def board_url(self, token, config):
        return f"https://{token}.breezy.hr/"


# --------------------------------------------------------------------------
# BambooHR
# --------------------------------------------------------------------------
class BambooHR(Connector):
    ats = "bamboohr"
    needs_detail = True

    def list_request(self, token, config):
        return Req(f"https://{token}.bamboohr.com/careers/list")

    def parse_list(self, data, token, config):
        out = []
        for j in data.get("result", []):
            loc = j.get("location") or {}
            parts = [loc.get("city"), loc.get("state"), loc.get("country")]
            out.append(RawJob(
                external_id=str(j["id"]),
                title=(j.get("jobOpeningName") or "").strip(),
                url=f"https://{token}.bamboohr.com/careers/{j['id']}",
                department=j.get("departmentLabel"),
                location=", ".join(x for x in parts if x) or j.get("locationType"),
                is_remote=str(j.get("isRemote") or "").lower() in ("1", "true", "yes"),
                employment_type=j.get("employmentStatusLabel"),
                posted_at=_iso(j.get("datePosted")),
                raw=j,
            ))
        return out

    def detail_request(self, job, token, config):
        return Req(f"https://{token}.bamboohr.com/careers/{job.external_id}/detail")

    def apply_detail(self, job, data):
        r = data.get("result") or data
        job.description_text = html_to_text(r.get("jobOpeningShareUrl") and r.get("description")
                                            or r.get("description"))
        job.raw = data

    def board_url(self, token, config):
        return f"https://{token}.bamboohr.com/careers"


# --------------------------------------------------------------------------
# Workable -- UNVERIFIED. Both the v1 widget and v3 POST endpoints returned
# 200 with zero results for every account tried on 2026-09-10, so treat this
# connector as best-effort until you confirm it against a live board.
# --------------------------------------------------------------------------
class Workable(Connector):
    ats = "workable"

    def list_request(self, token, config):
        return Req(f"https://apply.workable.com/api/v3/accounts/{token}/jobs",
                   method="POST",
                   json_body={"query": "", "location": [], "department": [],
                              "worktype": [], "remote": []})

    def parse_list(self, data, token, config):
        out = []
        for j in data.get("results", data.get("jobs", [])):
            out.append(RawJob(
                external_id=str(j.get("shortcode") or j.get("id")),
                title=(j.get("title") or "").strip(),
                url=j.get("url") or f"https://apply.workable.com/{token}/j/{j.get('shortcode')}/",
                department=j.get("department"),
                location=(j.get("location") or {}).get("city") if isinstance(j.get("location"), dict) else j.get("location"),
                is_remote=j.get("remote"),
                employment_type=j.get("type"),
                posted_at=_iso(j.get("published") or j.get("created_at")),
                description_text=html_to_text(j.get("description")),
                raw=j,
            ))
        return out

    def board_url(self, token, config):
        return f"https://apply.workable.com/{token}/"


# --------------------------------------------------------------------------
# Workday -- Tier 2, added for enterprise coverage. Structurally different from
# every Tier 1 board:
#   * POST, not GET, with a JSON body
#   * `limit` is hard-capped at 20 (50 and 100 both return HTTP 400), so a big
#     board like NVIDIA's 2,000 postings costs 100 paged requests
#   * no ETag and `cache-control: no-store`, so there is no conditional-GET
#     short-circuit -- every poll re-reads the whole board. Poll these less
#     often than Tier 1 boards.
#   * the list call's `postedOn` is relative prose ("Posted 30+ Days Ago"); the
#     real date lives in the detail payload as `startDate`.
#
# `config` carries {"host": "nvidia.wd5.myworkdayjobs.com", "site": "..."} --
# both are discovered once per company by `jobscraper workday-find`.
# --------------------------------------------------------------------------
class Workday(Connector):
    ats = "workday"
    needs_detail = True
    PAGE = 20              # server-enforced maximum
    page_size = 20
    MAX_PAGES = 400        # safety stop: 8,000 postings

    @staticmethod
    def _base(token: str, config: dict) -> str:
        host = config.get("host") or f"{token}.wd1.myworkdayjobs.com"
        site = config.get("site") or "External"
        return f"https://{host}/wday/cxs/{token}/{site}"

    def _body(self, offset: int) -> dict:
        return {"appliedFacets": {}, "limit": self.PAGE,
                "offset": offset, "searchText": ""}

    def list_request(self, token, config):
        return Req(f"{self._base(token, config)}/jobs", method="POST",
                   json_body=self._body(0))

    def next_request(self, data, token, config, fetched):
        # `total` is NOT trustworthy as a stop condition: several tenants report
        # exactly 2000 while still serving records at offset 2000, 2400 and
        # beyond. Stopping there truncated those boards, and because the cut
        # point drifts between polls, roles fell out of the window and were
        # recorded as closed. Page until the server returns a short, empty or
        # repeated page instead -- the pipeline detects all three.
        total = (data or {}).get("total") or 0
        if fetched >= self.PAGE * self.MAX_PAGES:
            return None
        if total and total % 1000 != 0 and fetched >= total:
            return None        # a plausible, non-round total we can trust
        return Req(f"{self._base(token, config)}/jobs", method="POST",
                   json_body=self._body(fetched))

    def parse_list(self, data, token, config):
        host = config.get("host") or f"{token}.wd1.myworkdayjobs.com"
        site = config.get("site") or "External"
        out = []
        for j in data.get("jobPostings", []):
            path = j.get("externalPath") or ""
            out.append(RawJob(
                external_id=path or str(j.get("bulletFields") or j.get("title")),
                title=(j.get("title") or "").strip(),
                url=f"https://{host}/en-US/{site}{path}",
                location=j.get("locationsText"),
                raw=j,
            ))
        return out

    def detail_request(self, job, token, config):
        if not job.external_id.startswith("/"):
            return None
        return Req(f"{self._base(token, config)}{job.external_id}")

    def apply_detail(self, job, data):
        info = data.get("jobPostingInfo") or {}
        job.description_text = html_to_text(info.get("jobDescription"))
        job.location = info.get("location") or job.location
        job.employment_type = info.get("timeType")
        # startDate is a real date; postedOn is prose like "Posted 30+ Days Ago".
        job.posted_at = _iso(info.get("startDate"))
        if info.get("remoteType"):
            job.is_remote = "remote" in str(info["remoteType"]).lower()
        job.url = info.get("externalUrl") or job.url
        job.raw = data

    def board_url(self, token, config):
        host = config.get("host") or f"{token}.wd1.myworkdayjobs.com"
        return f"https://{host}/en-US/{config.get('site') or 'External'}"


REGISTRY: dict[str, Connector] = {
    c.ats: c() for c in (Greenhouse, Ashby, Lever, SmartRecruiters,
                         Recruitee, Rippling, Breezy, BambooHR, Workable,
                         Workday)
}


def get(ats: str) -> Connector:
    try:
        return REGISTRY[ats]
    except KeyError:
        raise SystemExit(f"unknown ats {ats!r}; known: {', '.join(sorted(REGISTRY))}")
