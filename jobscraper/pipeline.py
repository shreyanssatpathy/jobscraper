"""Poll boards, diff against stored state, and enrich only the roles we want."""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import sqlite3
from dataclasses import asdict

import httpx

from . import db
from .classify import classify, is_remote
from .experience import extract_min_years
from .location import is_us as classify_us
from datetime import datetime, timezone, timedelta
from .connectors import RawJob, Req, get as get_connector

UA = "jobscraper/0.1 (personal job search; +contact: set-your-email-here)"

#: Postings older than this stop counting as matches. They are NOT deleted:
#: the row stays so close-detection keeps working and so a still-live posting is
#: never re-inserted as a fresh arrival on the next poll, which would put months
#: of backlog at the top of "newest arrivals". Set 0 to disable.
MAX_POSTING_AGE_DAYS = 30

#: How much a board is allowed to shrink in one poll before we refuse to close
#: anything. A truncated or errored response otherwise looks like a mass layoff.
SHRINK_GUARD = 0.5

MIN_INTERVAL = 900        # 15 min after a change
MAX_INTERVAL = 86_400     # 24 h for boards that never move
BACKOFF_FACTOR = 1.5


def too_old(posted_at: str | None, max_days: int = MAX_POSTING_AGE_DAYS) -> bool:
    """True when the company published this long enough ago to stop caring.

    An unknown date is never treated as old -- absence of evidence is not
    evidence the posting is stale.
    """
    if not max_days or not posted_at:
        return False
    try:
        when = datetime.fromisoformat(str(posted_at).replace("Z", "+00:00"))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when < datetime.now(timezone.utc) - timedelta(days=max_days)


def _hash_list(jobs: list[RawJob]) -> str:
    payload = sorted((j.external_id, j.title, j.location or "") for j in jobs)
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()


def _hash_job(j: RawJob) -> str:
    return hashlib.sha256(json.dumps([
        j.title, j.location, j.department, j.employment_type,
        j.salary_min, j.salary_max,
        (j.description_text or "")[:4000],
    ], default=str).encode()).hexdigest()


class Poller:
    def __init__(self, conn: sqlite3.Connection, *, threshold: float = 1.0,
                 concurrency: int = 8, per_host: int = 2, dry_run: bool = False,
                 verbose: bool = True):
        self.conn = conn
        # One identifier for this whole invocation, so every posting records
        # which refresh introduced it rather than only the calendar day.
        self.batch = db.now()
        self.threshold = threshold
        self.dry_run = dry_run
        self.verbose = verbose
        self.sem = asyncio.Semaphore(concurrency)
        self.host_sems: dict[str, asyncio.Semaphore] = {}
        self.per_host = per_host
        self.stats = {"sources": 0, "listed": 0, "matched": 0, "new": 0,
                      "updated": 0, "closed": 0, "errors": 0, "skipped": 0}

    # ---------------- HTTP ----------------
    def _host_sem(self, url: str) -> asyncio.Semaphore:
        host = httpx.URL(url).host
        if host not in self.host_sems:
            self.host_sems[host] = asyncio.Semaphore(self.per_host)
        return self.host_sems[host]

    async def _fetch(self, client: httpx.AsyncClient, req: Req,
                     etag: str | None = None) -> httpx.Response:
        headers = {"User-Agent": UA, "Accept": "application/json"}
        if etag:
            headers["If-None-Match"] = etag
        async with self.sem, self._host_sem(req.url):
            for attempt in range(4):
                try:
                    r = await client.request(req.method, req.url, json=req.json_body,
                                             headers=headers, timeout=45)
                except (httpx.TimeoutException, httpx.TransportError):
                    if attempt == 3:
                        raise
                    await asyncio.sleep(2 ** attempt + random.random())
                    continue
                if r.status_code in (429, 502, 503, 504):
                    wait = float(r.headers.get("Retry-After", 2 ** attempt))
                    if attempt == 3:
                        r.raise_for_status()
                    await asyncio.sleep(wait + random.random())
                    continue
                await asyncio.sleep(0.4 + random.random() * 0.4)   # be a polite guest
                return r
        raise RuntimeError("unreachable")

    # ---------------- per-source poll ----------------
    async def poll_source(self, client: httpx.AsyncClient, src: sqlite3.Row) -> None:
        conn, sid = self.conn, src["id"]
        conn_ats, token = src["ats"], src["token"]
        config = json.loads(src["config"] or "{}")
        connector = get_connector(conn_ats)
        self.stats["sources"] += 1

        try:
            r = await self._fetch(client, connector.list_request(token, config), src["last_etag"])
            if r.status_code == 304:
                self._finish(src, "not_modified", grow=True)
                self.stats["skipped"] += 1
                self._log(f"  {src['company']:<24} {conn_ats:<15} 304 not modified")
                return
            r.raise_for_status()
            first_payload = r.json()
            jobs = connector.parse_list(first_payload, token, config)
            # Paged boards (Workday) hand back one page at a time. Only the
            # FIRST response carries the row count -- Workday reports total=0 on
            # every subsequent page -- so next_request always sees page one.
            seen_ids = {j.external_id for j in jobs}
            while True:
                nxt = connector.next_request(first_payload, token, config, len(jobs))
                if nxt is None:
                    break
                page = await self._fetch(client, nxt)
                page.raise_for_status()
                batch = connector.parse_list(page.json(), token, config)
                if not batch:                       # server ran out of rows
                    break
                fresh = [j for j in batch if j.external_id not in seen_ids]
                if not fresh:                       # server clamped the offset
                    break
                seen_ids.update(j.external_id for j in fresh)
                jobs.extend(fresh)
                if connector.page_size and len(batch) < connector.page_size:
                    break                           # short page = last page
        except Exception as exc:                                  # noqa: BLE001
            self.stats["errors"] += 1
            conn.execute(
                """UPDATE source SET last_polled_at=?, consecutive_failures=consecutive_failures+1,
                          last_error=?, enabled = CASE WHEN consecutive_failures+1 >= 10 THEN 0 ELSE enabled END
                   WHERE id=?""", (db.now(), f"{type(exc).__name__}: {exc}"[:300], sid))
            conn.execute(
                "INSERT INTO poll_run (source_id, at, status, detail) VALUES (?,?,?,?)",
                (sid, db.now(), "error", f"{type(exc).__name__}: {exc}"[:300]))
            conn.commit()
            self._log(f"  {src['company']:<24} {conn_ats:<15} ERROR {type(exc).__name__}: {str(exc)[:70]}")
            return

        etag = r.headers.get("etag")
        listed = len(jobs)
        self.stats["listed"] += listed
        list_hash = _hash_list(jobs)

        if list_hash == src["last_hash"]:
            self._finish(src, "unchanged", etag=etag, grow=True, count=listed)
            self.stats["skipped"] += 1
            self._log(f"  {src['company']:<24} {conn_ats:<15} unchanged ({listed} jobs)")
            return

        # Classify on the title first: only matches earn a detail fetch.
        verdicts = [(j, classify(j.title, j.department, j.team, self.threshold))
                    for j in jobs]
        matches = [(j, m) for j, m in verdicts if m.matched]
        self.stats["matched"] += len(matches)

        existing = db.open_jobs(conn, sid)
        needs_detail = connector.needs_detail
        # Enrich a match when it is new, or when it is already stored but has no
        # description -- the latter happens after `reclassify` widens the
        # taxonomy and previously-ignored postings become matches.
        to_enrich = [(j, m) for j, m in matches
                     if needs_detail and (j.external_id not in existing
                                          or not existing[j.external_id]["description_text"])]
        if to_enrich and not self.dry_run:
            await asyncio.gather(*(self._enrich(client, connector, j, token, config)
                                   for j, _ in to_enrich), return_exceptions=True)

        if self.dry_run:
            self._log(f"  {src['company']:<24} {conn_ats:<15} {listed:>4} listed  "
                      f"{len(matches):>3} match  (dry run)")
            for j, m in matches[:8]:
                self._log(f"       · [{m.category}/{m.seniority}] {j.title}")
            return

        new_ct = upd_ct = 0
        seen_ids = set()
        for j, m in verdicts:
            seen_ids.add(j.external_id)
            kind = self._upsert(sid, src["company"], j, m)
            if kind == "new":
                new_ct += 1
            elif kind == "updated":
                upd_ct += 1

        # Close what vanished -- unless the board shrank implausibly, which
        # almost always means a truncated response rather than 400 closed reqs.
        closed_ct = 0
        gone = set(existing) - seen_ids
        prev = src["last_job_count"] or 0
        suspect = listed == 0 or (prev and listed < prev * SHRINK_GUARD)
        if gone and not suspect:
            for ext_id in gone:
                row = existing[ext_id]
                conn.execute("UPDATE job SET closed_at=? WHERE id=?", (db.now(), row["id"]))
                db.record_event(conn, row["id"], "closed")
                closed_ct += 1

        self.stats["new"] += new_ct
        self.stats["updated"] += upd_ct
        self.stats["closed"] += closed_ct
        changed = bool(new_ct or upd_ct or closed_ct)
        self._finish(src, "suspect" if suspect else "ok", etag=etag, grow=not changed,
                     count=listed, matched=len(matches), new=new_ct, closed=closed_ct,
                     list_hash=list_hash)

        flag = "  ⚠ shrink-guard tripped, skipped closes" if suspect and gone else ""
        self._log(f"  {src['company']:<24} {conn_ats:<15} {listed:>4} listed  "
                  f"{len(matches):>3} match  +{new_ct} new  ~{upd_ct} upd  -{closed_ct} closed{flag}")

    async def _enrich(self, client, connector, job: RawJob, token: str, config: dict) -> None:
        req = connector.detail_request(job, token, config)
        if not req:
            return
        try:
            r = await self._fetch(client, req)
            r.raise_for_status()
            connector.apply_detail(job, r.json())
        except Exception:                                          # noqa: BLE001
            pass    # a missing description never blocks storing the posting

    # ---------------- persistence ----------------
    def _upsert(self, source_id: int, company: str, j: RawJob, m) -> str:
        """Insert or refresh one posting.

        Detail-only fields are COALESCEd on update. Workday's list call carries
        no posting date (only prose like "Posted 30+ Days Ago"), and a match
        that already has a description is not re-enriched -- so assigning these
        fields unconditionally wrote NULL over good data on every re-poll,
        erasing the dates every date filter depends on.
        """
        conn = self.conn
        chash = _hash_job(j)
        remote = j.is_remote
        if remote is None:
            remote = is_remote(j.location, j.title)
        row = conn.execute(
            "SELECT id, content_hash, closed_at FROM job WHERE source_id=? AND external_id=?",
            (source_id, j.external_id)).fetchone()

        us = classify_us(j.location)
        # Age gate sits here, after enrichment, because Workday's posting date
        # only arrives with the detail payload.
        matched = bool(m.matched) and not too_old(j.posted_at)
        fields = (j.url, j.title, j.department, j.team, j.location,
                  int(remote) if remote is not None else None,
                  int(us) if us is not None else None, j.employment_type,
                  j.salary_min, j.salary_max, j.salary_currency, j.salary_raw,
                  j.description_text, j.posted_at,
                  m.category, m.seniority, extract_min_years(j.description_text),
                  m.score, int(matched),
                  chash, json.dumps(j.raw) if m.matched else None)

        if row is None:
            cur = conn.execute(
                """INSERT INTO job (source_id, external_id, url, title, department, team,
                       location, is_remote, is_us, employment_type, salary_min, salary_max,
                       salary_currency, salary_raw, description_text, posted_at,
                       role_category, seniority, min_years_exp, match_score, is_match,
                       content_hash, raw, first_seen_at, first_seen_batch, last_seen_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id""",
                (source_id, j.external_id, *fields, db.now(), self.batch, db.now()))
            jid = cur.fetchone()[0]
            if matched:
                db.record_event(conn, jid, "new", f"{m.category}/{m.seniority}")
                db.index_fts(conn, jid, j.title, j.description_text, company)
            return "new"

        reopened = row["closed_at"] is not None
        changed = row["content_hash"] != chash
        conn.execute(
            """UPDATE job SET url=?, title=?, department=?, team=?, location=?,
                   is_remote=COALESCE(?, is_remote), is_us=COALESCE(?, is_us),
                   employment_type=COALESCE(?, employment_type),
                   salary_min=COALESCE(?, salary_min), salary_max=COALESCE(?, salary_max),
                   salary_currency=COALESCE(?, salary_currency),
                   salary_raw=COALESCE(?, salary_raw),
                   description_text=COALESCE(?, description_text),
                   posted_at=COALESCE(?, posted_at),
                   role_category=?, seniority=?, min_years_exp=COALESCE(?, min_years_exp),
                   match_score=?, is_match=?, content_hash=?,
                   raw=COALESCE(?, raw), last_seen_at=?, closed_at=NULL
               WHERE id=?""", (*fields, db.now(), row["id"]))
        if matched and reopened:
            db.record_event(conn, row["id"], "reopened")
        elif matched and changed:
            db.record_event(conn, row["id"], "updated")
        return "updated" if changed else "seen"

    def _finish(self, src, status, *, etag=None, grow=False, count=None,
                matched=0, new=0, closed=0, list_hash=None) -> None:
        interval = src["poll_interval_s"] or 3600
        interval = (min(int(interval * BACKOFF_FACTOR), MAX_INTERVAL) if grow
                    else MIN_INTERVAL)
        self.conn.execute(
            """UPDATE source SET last_polled_at=?, last_etag=COALESCE(?, last_etag),
                   last_hash=COALESCE(?, last_hash), last_job_count=COALESCE(?, last_job_count),
                   poll_interval_s=?, consecutive_failures=0, last_error=NULL
               WHERE id=?""",
            (db.now(), etag, list_hash, count, interval, src["id"]))
        self.conn.execute(
            """INSERT INTO poll_run (source_id, at, status, listed, matched, new_jobs, closed_jobs)
               VALUES (?,?,?,?,?,?,?)""",
            (src["id"], db.now(), status, count or 0, matched, new, closed))
        self.conn.commit()

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)

    async def run(self, sources: list[sqlite3.Row]) -> dict:
        limits = httpx.Limits(max_connections=20, max_keepalive_connections=10)
        async with httpx.AsyncClient(follow_redirects=True, limits=limits) as client:
            await asyncio.gather(*(self.poll_source(client, s) for s in sources))
        return self.stats


async def enrich_missing(conn, *, limit: int = 2000, verbose: bool = True) -> int:
    """Fetch descriptions for matched postings that lack one.

    `poll` short-circuits on ETag/hash, so a board that has not changed is never
    re-read -- which leaves postings that only became matches after
    `reclassify` without a description. This fills them in directly, bypassing
    the change-detection path entirely.
    """
    rows = conn.execute("""
        SELECT j.id, j.external_id, j.title, s.ats, s.token, s.config
        FROM job j JOIN source s ON s.id = j.source_id
        WHERE j.is_match = 1 AND j.closed_at IS NULL
          AND (j.description_text IS NULL OR j.posted_at IS NULL)
        LIMIT ?""", (limit,)).fetchall()
    if not rows:
        if verbose:
            print("nothing to enrich")
        return 0

    poller = Poller(conn, verbose=False)
    done = 0
    limits = httpx.Limits(max_connections=20, max_keepalive_connections=10)
    async with httpx.AsyncClient(follow_redirects=True, limits=limits) as client:
        async def one(row):
            nonlocal done
            connector = get_connector(row["ats"])
            config = json.loads(row["config"] or "{}")
            job = RawJob(external_id=row["external_id"], title=row["title"])
            req = connector.detail_request(job, row["token"], config)
            if req is None:
                return
            try:
                r = await poller._fetch(client, req)
                r.raise_for_status()
                connector.apply_detail(job, r.json())
            except Exception:                                      # noqa: BLE001
                return
            if not (job.description_text or job.posted_at):
                return
            conn.execute(
                """UPDATE job SET description_text=COALESCE(?, description_text),
                       min_years_exp=COALESCE(?, min_years_exp),
                       posted_at=COALESCE(?, posted_at),
                       employment_type=COALESCE(?, employment_type),
                       raw=COALESCE(?, raw)
                   WHERE id=?""",
                (job.description_text, extract_min_years(job.description_text),
                 job.posted_at, job.employment_type,
                 json.dumps(job.raw) if job.raw else None, row["id"]))
            done += 1

        await asyncio.gather(*(one(r) for r in rows), return_exceptions=True)
    conn.commit()
    if verbose:
        print(f"enriched {done} of {len(rows)} postings")
    return done
