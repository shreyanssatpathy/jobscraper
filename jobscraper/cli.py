"""Command line interface."""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import sys
from pathlib import Path

import httpx
import yaml

from . import db
from .classify import classify
from .connectors import REGISTRY, get as get_connector
from .pipeline import Poller, UA, enrich_missing
from .lock import Locked, poll_lock

CATEGORIES = ("data_engineer", "data_scientist", "ai_engineer", "analytics")

_WINDOW = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([hdw])\s*$", re.I)


def parse_window(value: str) -> str:
    """'24h' / '48h' / '7d' / '2w' -> a SQLite datetime modifier."""
    m = _WINDOW.match(value)
    if not m:
        raise argparse.ArgumentTypeError(
            f"bad window {value!r}; use e.g. 24h, 48h, 7d, 2w")
    n, unit = float(m.group(1)), m.group(2).lower()
    hours = n * {"h": 1, "d": 24, "w": 168}[unit]
    return f"-{hours} hours"

# Regexes that map a company careers page to its ATS + board token.
SNIFF = [
    ("greenhouse",      r"(?:job-)?boards(?:-api)?\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)"),
    ("greenhouse",      r"greenhouse\.io/embed/job_board\?for=([A-Za-z0-9_-]+)"),
    ("ashby",           r"jobs\.ashbyhq\.com/([A-Za-z0-9_.-]+)"),
    ("ashby",           r"api\.ashbyhq\.com/posting-api/job-board/([A-Za-z0-9_.-]+)"),
    ("lever",           r"jobs\.lever\.co/([A-Za-z0-9_-]+)"),
    ("lever",           r"api\.lever\.co/v0/postings/([A-Za-z0-9_-]+)"),
    ("smartrecruiters", r"careers\.smartrecruiters\.com/([A-Za-z0-9_-]+)"),
    ("recruitee",       r"([A-Za-z0-9_-]+)\.recruitee\.com"),
    ("rippling",        r"ats\.rippling\.com/([A-Za-z0-9_-]+)"),
    ("breezy",          r"([A-Za-z0-9_-]+)\.breezy\.hr"),
    ("bamboohr",        r"([A-Za-z0-9_-]+)\.bamboohr\.com/careers"),
    ("workable",        r"apply\.workable\.com/([A-Za-z0-9_-]+)"),
]

#: Workday needs host + site, not just a token, so it is matched separately.
WORKDAY_URL = re.compile(
    r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com"
    r"/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)", re.I)

WD_HOSTS = ["wd1", "wd3", "wd5", "wd2", "wd12", "wd101", "wd103", "wd105"]


def _site_guesses(tenant: str) -> list[str]:
    """Site-name candidates, ordered by how often they hit in practice.

    The lowercase and `Jobsat<tenant>` forms were learned the hard way: Adobe
    uses `external_experienced`, HPE `Jobsathpe`, Palo Alto Networks
    `panwexternalcareers` and Broadcom `External_Career` (singular). None of
    those were guessable from the tenant alone, so `discover` reads them off the
    careers page where it can.
    """
    t, T, U = tenant, tenant.capitalize(), tenant.upper()
    return [f"{U}ExternalCareerSite", "External", "external", "Careers", "careers",
            "External_Career_Site", "External_Career", "ExternalCareerSite",
            f"{T}Careers", f"{t}careers", f"{t}externalcareers", f"{t}jobs",
            f"{T}_Careers", t, f"{T}External", f"{U}_External_Career_Site",
            f"{T}ExternalCareerSite", "ExternalCareers", "external_experienced",
            "external_career", "Global", "GlobalCareers", "CareerSite",
            f"Jobsat{t}", f"jobsat{t}", f"{T}_Careers_External", f"{U}Careers",
            "EXT", "Ext", "Search", "jobs", "Professional", "Recruiting"]


def cmd_workday_find(args, conn):
    """Resolve a Workday tenant to its host + site.

    Workday answers 422 on the wrong host, 404 on the right host with a wrong
    site name, and 200 when both are right -- so the host can be identified
    before any site name is guessed.
    """
    tenant = args.tenant
    body = {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}
    hdr = {"User-Agent": UA, "Accept": "application/json"}

    host = None
    for wd in WD_HOSTS:
        candidate = f"{tenant}.{wd}.myworkdayjobs.com"
        try:
            r = httpx.post(f"https://{candidate}/wday/cxs/{tenant}/__probe__/jobs",
                           json=body, headers=hdr, timeout=15)
        except Exception:                                          # noqa: BLE001
            continue
        if r.status_code == 404:        # right host, wrong site
            host = candidate
            break
    if not host:
        print(f"✗ {tenant}: no Workday tenant found "
              f"(tried {', '.join(WD_HOSTS)}) — the company may not use Workday, "
              f"or its tenant name differs from {tenant!r}")
        return 1

    for site in _site_guesses(tenant):
        try:
            r = httpx.post(f"https://{host}/wday/cxs/{tenant}/{site}/jobs",
                           json=body, headers=hdr, timeout=15)
        except Exception:                                          # noqa: BLE001
            continue
        if r.status_code == 200 and "jobPostings" in r.text:
            total = r.json().get("total")
            print(f"✓ {tenant}: {host}  site={site}  ({total} postings)")
            if args.add:
                db.add_source(conn, "workday", tenant, args.company or tenant.title(),
                              config={"host": host, "site": site},
                              poll_interval_s=args.interval)
                print(f"  added as source workday/{tenant}")
            else:
                print(f"  jobscraper workday-find {tenant} --add")
            return 0
    print(f"~ {tenant}: host is {host} but no site name matched.\n"
          f"  Open the company's Workday careers page and read the site from the\n"
          f"  URL (https://{host}/en-US/<SITE>), then:\n"
          f"  jobscraper add workday {tenant} --company NAME --wd-host {host} --wd-site <SITE>")
    return 1


def cmd_add(args, conn):
    get_connector(args.ats)
    name = args.company or args.token
    if db.is_blocked(name, db.load_blocklist()):
        raise SystemExit(f"{name!r} is in blocklist.yaml — remove it there first")
    config = {}
    if args.ats == "workday":
        if not (args.wd_host and args.wd_site):
            raise SystemExit("workday needs --wd-host and --wd-site; "
                             "run `jobscraper workday-find <tenant>` to resolve them")
        config = {"host": args.wd_host, "site": args.wd_site}
    sid = db.add_source(conn, args.ats, args.token, args.company or args.token,
                        config=config, poll_interval_s=args.interval)
    print(f"source #{sid}: {args.ats}/{args.token}")


def cmd_import(args, conn):
    data = yaml.safe_load(Path(args.file).read_text())
    blocked = db.load_blocklist()
    n = skipped = 0
    for entry in data.get("sources", []):
        if db.is_blocked(entry.get("company") or entry["token"], blocked):
            skipped += 1
            continue
        get_connector(entry["ats"])
        db.add_source(conn, entry["ats"], entry["token"],
                      entry.get("company") or entry["token"],
                      config=entry.get("config"),
                      h1b_approvals=entry.get("h1b_approvals"),
                      poll_interval_s=entry.get(
                          "poll_interval_s",
                          21600 if entry["ats"] == "workday" else 3600))
        n += 1
    msg = f"imported {n} sources from {args.file}"
    if skipped:
        msg += f" ({skipped} skipped by blocklist.yaml)"
    print(msg)


def cmd_sources(args, conn):
    rows = conn.execute("""
        SELECT s.*, (SELECT COUNT(*) FROM job j WHERE j.source_id=s.id AND j.is_match=1
                     AND j.closed_at IS NULL) AS matches
        FROM source s ORDER BY s.enabled DESC, matches DESC, s.company""").fetchall()
    print(f"{'id':>4}  {'company':<26} {'ats':<16} {'tok':<22} {'open':>5} {'every':>7}  last poll")
    for r in rows:
        flag = "" if r["enabled"] else "  [disabled]"
        err = f"  !{r['last_error'][:40]}" if r["last_error"] else ""
        print(f"{r['id']:>4}  {r['company'][:26]:<26} {r['ats']:<16} {r['token'][:22]:<22} "
              f"{r['matches']:>5} {r['poll_interval_s']//60:>5}m  {(r['last_polled_at'] or '-')[:19]}{flag}{err}")
    print(f"\n{len(rows)} sources")


def cmd_check(args, conn):
    """Validate a board token cheaply before adding it."""
    connector = get_connector(args.ats)
    req = connector.list_request(args.token, {})
    r = httpx.request(req.method, req.url, json=req.json_body,
                      headers={"User-Agent": UA, "Accept": "application/json"},
                      timeout=30, follow_redirects=True)
    if r.status_code != 200:
        print(f"✗ {args.ats}/{args.token}: HTTP {r.status_code}")
        return 1
    jobs = connector.parse_list(r.json(), args.token, {})
    hits = [(j, classify(j.title, j.department, j.team)) for j in jobs]
    hits = [(j, m) for j, m in hits if m.matched]
    # SmartRecruiters answers 200/empty for unknown companies, so zero jobs is
    # ambiguous rather than a clean pass.
    mark = "✓" if jobs else "?"
    print(f"{mark} {args.ats}/{args.token}: {len(jobs)} postings, {len(hits)} matching your roles")
    for j, m in hits[:15]:
        print(f"    [{m.category:<14} {m.seniority:<8}] {j.title}  — {j.location or '?'}")
    return 0


def cmd_discover(args, conn):
    """Fetch a careers page and identify which ATS backs it."""
    url = args.url if args.url.startswith("http") else f"https://{args.url}"
    found: dict[tuple[str, str], int] = {}
    for candidate in (url, url.rstrip("/") + "/careers", url.rstrip("/") + "/jobs"):
        try:
            r = httpx.get(candidate, headers={"User-Agent": UA}, timeout=25, follow_redirects=True)
        except Exception:                                          # noqa: BLE001
            continue
        body = r.text
        for ats, pattern in SNIFF:
            for m in re.finditer(pattern, body, re.I):
                token = m.group(1)
                if token.lower() in ("www", "jobs", "careers", "api", "embed", "apply"):
                    continue
                found[(ats, token)] = found.get((ats, token), 0) + 1
    wd = {}
    for candidate in (url, url.rstrip("/") + "/careers", url.rstrip("/") + "/jobs"):
        try:
            r = httpx.get(candidate, headers={"User-Agent": UA}, timeout=25, follow_redirects=True)
        except Exception:                                          # noqa: BLE001
            continue
        for m in WORKDAY_URL.finditer(r.text + " " + str(r.url)):
            if m.group(3).lower() not in ("login", "home", "wday", "candidate"):
                wd[(m.group(1), m.group(2), m.group(3))] = 1
    for tenant, host_id, site in wd:
        print(f"  {'workday':<16} {tenant:<24} host={tenant}.{host_id}.myworkdayjobs.com site={site}")
        print(f"  {'':<16} →  jobscraper add workday {tenant} "
              f"--wd-host {tenant}.{host_id}.myworkdayjobs.com --wd-site {site}")
        if args.add:
            db.add_source(conn, "workday", tenant, args.company or tenant.title(),
                          config={"host": f"{tenant}.{host_id}.myworkdayjobs.com", "site": site},
                          poll_interval_s=21600)

    if not found:
        if wd:
            return 0
        print(f"no ATS fingerprint found on {url}")
        return 1
    for (ats, token), hits in sorted(found.items(), key=lambda kv: -kv[1]):
        print(f"  {ats:<16} {token:<24} ({hits} refs)   →  jobscraper add {ats} {token}")
    if args.add:
        (ats, token), _ = max(found.items(), key=lambda kv: kv[1])
        db.add_source(conn, ats, token, args.company or token)
        print(f"added {ats}/{token}")
    return 0


def cmd_poll(args, conn):
    sources = db.due_sources(conn, force=args.force)
    if args.only:
        sources = [s for s in sources if s["ats"] in args.only]
    if args.company:
        needle = args.company.lower()
        sources = [s for s in sources if needle in s["company"].lower() or needle in s["token"].lower()]
    if not sources:
        print("no sources due (use --force to poll everything)")
        return 0
    print(f"polling {len(sources)} sources…")
    poller = Poller(conn, threshold=args.threshold, concurrency=args.concurrency,
                    dry_run=args.dry_run)
    try:
        with poll_lock(wait=args.lock_wait):
            stats = asyncio.run(poller.run(sources))
    except Locked as exc:
        # Exit 0 so a scheduled run that simply had to yield is not reported as
        # a failure; the next tick picks the work up.
        print(f"skipped: {exc}")
        return 0
    print("\n" + "  ".join(f"{k}={v}" for k, v in stats.items()))
    return 0


def _job_filters(args):
    where = ["j.closed_at IS NULL", "j.is_match = 1"]
    params: list = []
    # --within filters on the company's own posting date; --seen-within on when
    # this scraper first saw it. They answer different questions, so both exist.
    if getattr(args, "within", None):
        where.append("j.posted_at IS NOT NULL AND j.posted_at >= datetime('now', ?)")
        params.append(args.within)
    if getattr(args, "seen_within", None):
        where.append("j.first_seen_at >= datetime('now', ?)")
        params.append(args.seen_within)
    if getattr(args, "max_exp", None) is not None:
        # Postings that state no requirement are kept unless --strict-exp.
        if getattr(args, "strict_exp", False):
            where.append("j.min_years_exp IS NOT NULL AND j.min_years_exp <= ?")
        else:
            where.append("(j.min_years_exp IS NULL OR j.min_years_exp <= ?)")
        params.append(args.max_exp)
    if args.category:
        where.append(f"j.role_category IN ({','.join('?' * len(args.category))})")
        params += args.category
    if args.seniority:
        where.append(f"j.seniority IN ({','.join('?' * len(args.seniority))})")
        params += args.seniority
    if getattr(args, "full_time", False):
        # Two independent signals. Seniority comes from a word-boundary match on
        # the title, so "International" and "Internal" are safe. employment_type
        # is free text and null on ~43% of postings, so nulls are KEPT -- a
        # missing value is not evidence of a part-time role.
        where.append("COALESCE(j.seniority,'') != 'intern'")
        where.append("""(j.employment_type IS NULL OR (
                 lower(j.employment_type) NOT LIKE '%intern%'
             AND lower(j.employment_type) NOT LIKE '%part%time%'
             AND lower(j.employment_type) NOT LIKE '%contract%'
             AND lower(j.employment_type) NOT LIKE '%tempor%'
             AND lower(j.employment_type) NOT LIKE '%seasonal%'
             AND lower(j.employment_type) NOT LIKE '%co-op%'
             AND lower(j.employment_type) NOT LIKE '%co op%'))""")
    if getattr(args, "sponsor", False):
        where.append("s.h1b_approvals IS NOT NULL")
    if getattr(args, "min_approvals", None):
        where.append("s.h1b_approvals >= ?")
        params.append(args.min_approvals)
    if getattr(args, "us", False):
        # Unknown locations are excluded here: "US only" should mean confirmed
        # US, not "not obviously foreign".
        where.append("j.is_us = 1")
    if getattr(args, "us_or_unknown", False):
        where.append("(j.is_us = 1 OR j.is_us IS NULL)")
    if args.remote:
        where.append("j.is_remote = 1")
    if args.location:
        where.append("j.location LIKE ?")
        params.append(f"%{args.location}%")
    return " AND ".join(where), params


def _print_jobs(rows, show_url=True):
    for r in rows:
        sal = ""
        if r["salary_min"]:
            sal = f"  ${r['salary_min']/1000:.0f}K–${(r['salary_max'] or 0)/1000:.0f}K"
        exp = (f"  ·  {r['min_years_exp']}+ yrs req"
               if r["min_years_exp"] is not None else "  ·  yrs not stated")
        try:
            spon = f"  ·  H1B {r['h1b_approvals']:,}" if r["h1b_approvals"] else ""
        except (IndexError, KeyError):
            spon = ""
        posted = f"posted {r['posted_at'][:10]}" if r["posted_at"] else "posted date unknown"
        print(f"\n  {r['title']}")
        print(f"    {r['company']}  ·  {r['location'] or 'n/a'}  ·  "
              f"{r['role_category']}/{r['seniority']}{sal}{exp}{spon}")
        print(f"    {posted}  ·  found {r['first_seen_at'][:10]}" +
              (f"\n    {r['url']}" if show_url and r["url"] else ""))


def cmd_new(args, conn):
    where, params = _job_filters(args)
    rows = conn.execute(f"""
        SELECT j.*, s.company, s.h1b_approvals FROM job j JOIN source s ON s.id=j.source_id
        WHERE {where} ORDER BY COALESCE(j.posted_at, j.first_seen_at) DESC LIMIT ?""",
        (*params, args.limit)).fetchall()
    _print_jobs(rows)
    print(f"\n{len(rows)} postings")
    return 0


def cmd_search(args, conn):
    where, params = _job_filters(args)
    like = f"%{args.query}%"
    rows = conn.execute(f"""
        SELECT j.*, s.company, s.h1b_approvals FROM job j JOIN source s ON s.id=j.source_id
        WHERE {where} AND (j.title LIKE ? OR j.description_text LIKE ?)
        ORDER BY j.match_score DESC, j.first_seen_at DESC LIMIT ?""",
        (*params, like, like, args.limit)).fetchall()
    _print_jobs(rows)
    print(f"\n{len(rows)} postings matching {args.query!r}")
    return 0


def cmd_export(args, conn):
    where, params = _job_filters(args)
    rows = conn.execute(f"""
        SELECT s.company, s.h1b_approvals, j.title, j.role_category, j.seniority, j.min_years_exp,
               j.location, j.is_us, j.is_remote, j.salary_min, j.salary_max, j.salary_currency,
               j.employment_type, j.posted_at, j.first_seen_at, j.url
        FROM job j JOIN source s ON s.id=j.source_id
        WHERE {where} ORDER BY COALESCE(j.posted_at, j.first_seen_at) DESC""", params).fetchall()
    out = Path(args.out)
    if out.suffix == ".json":
        out.write_text(json.dumps([dict(r) for r in rows], indent=2))
    else:
        with out.open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(rows[0].keys() if rows else ["company", "title"])
            w.writerows([tuple(r) for r in rows])
    print(f"wrote {len(rows)} rows to {out}")
    return 0


def cmd_reclassify(args, conn):
    """Re-run the classifier over stored postings.

    Titles are stored for every posting, so widening the taxonomy does not need
    a re-crawl. Newly matched postings have no description yet -- the next
    `poll` fills those in.
    """
    from .classify import classify as _classify
    from .experience import extract_min_years

    rows = conn.execute("SELECT id, title, department, team, description_text, "
                        "is_match FROM job WHERE closed_at IS NULL").fetchall()
    gained = lost = 0
    for r in rows:
        m = _classify(r["title"], r["department"], r["team"], args.threshold)
        if bool(m.matched) != bool(r["is_match"]):
            gained += bool(m.matched)
            lost += bool(r["is_match"])
        conn.execute(
            """UPDATE job SET role_category=?, seniority=?, match_score=?, is_match=?,
                   min_years_exp=COALESCE(min_years_exp, ?) WHERE id=?""",
            (m.category, m.seniority, m.score, int(m.matched),
             extract_min_years(r["description_text"]), r["id"]))
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM job WHERE is_match=1 AND closed_at IS NULL").fetchone()[0]
    missing = conn.execute("SELECT COUNT(*) FROM job WHERE is_match=1 AND closed_at IS NULL "
                           "AND description_text IS NULL").fetchone()[0]
    print(f"rescored {len(rows)} postings: +{gained} newly matched, -{lost} no longer matched")
    print(f"matching roles now: {total}")
    if missing:
        print(f"{missing} of them have no description yet — run `jobscraper enrich`")
    return 0


def cmd_enrich(args, conn):
    return 0 if asyncio.run(enrich_missing(conn, limit=args.limit)) >= 0 else 1


def cmd_dashboard(args, conn):
    from .dashboard import build
    out, n = build(conn, args.out)
    print(f"wrote {out} with {n} roles ({out.stat().st_size/1024/1024:.2f} MB)")
    return 0


def cmd_stats(args, conn):
    q = lambda sql: conn.execute(sql).fetchall()                    # noqa: E731
    total, matched, closed = q("""SELECT COUNT(*), SUM(is_match),
        SUM(closed_at IS NOT NULL) FROM job""")[0]
    print(f"jobs tracked: {total}   matching your roles: {matched or 0}   closed: {closed or 0}\n")
    print("by category (open):")
    for r in q("""SELECT role_category, COUNT(*) n FROM job
                  WHERE is_match=1 AND closed_at IS NULL
                  GROUP BY 1 ORDER BY n DESC"""):
        print(f"    {r[0]:<16} {r[1]}")
    print("\nby seniority (open):")
    for r in q("""SELECT seniority, COUNT(*) n FROM job
                  WHERE is_match=1 AND closed_at IS NULL GROUP BY 1 ORDER BY n DESC"""):
        print(f"    {r[0]:<16} {r[1]}")
    print("\nrecent activity:")
    for r in q("""SELECT kind, COUNT(*) n FROM event
                  WHERE at >= datetime('now','-7 days') GROUP BY 1 ORDER BY n DESC"""):
        print(f"    {r[0]:<16} {r[1]} (7d)")
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="jobscraper",
                                description="Poll Tier 1 ATS boards for data/AI roles.")
    p.add_argument("--db", default="jobs.db")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="add a board")
    a.add_argument("ats", choices=sorted(REGISTRY))
    a.add_argument("token")
    a.add_argument("--company")
    a.add_argument("--interval", type=int, default=3600)
    a.add_argument("--wd-host", help="workday only, e.g. nvidia.wd5.myworkdayjobs.com")
    a.add_argument("--wd-site", help="workday only, e.g. NVIDIAExternalCareerSite")
    a.set_defaults(fn=cmd_add)

    a = sub.add_parser("workday-find", help="resolve a Workday tenant to host + site")
    a.add_argument("tenant")
    a.add_argument("--add", action="store_true")
    a.add_argument("--company")
    a.add_argument("--interval", type=int, default=21600)
    a.set_defaults(fn=cmd_workday_find)

    a = sub.add_parser("import", help="bulk-add boards from a YAML file")
    a.add_argument("file")
    a.set_defaults(fn=cmd_import)

    a = sub.add_parser("sources", help="list configured boards")
    a.set_defaults(fn=cmd_sources)

    a = sub.add_parser("check", help="probe a board without storing anything")
    a.add_argument("ats", choices=sorted(REGISTRY))
    a.add_argument("token")
    a.set_defaults(fn=cmd_check)

    a = sub.add_parser("discover", help="identify the ATS behind a careers page")
    a.add_argument("url")
    a.add_argument("--add", action="store_true")
    a.add_argument("--company")
    a.set_defaults(fn=cmd_discover)

    a = sub.add_parser("poll", help="fetch boards and record changes")
    a.add_argument("--force", action="store_true", help="ignore poll intervals")
    a.add_argument("--dry-run", action="store_true")
    a.add_argument("--only", nargs="+", choices=sorted(REGISTRY))
    a.add_argument("--company")
    a.add_argument("--threshold", type=float, default=1.0)
    a.add_argument("--concurrency", type=int, default=8)
    a.add_argument("--lock-wait", type=float, default=900, metavar="SECONDS",
                   help="how long to wait for a concurrent poll to finish "
                        "before skipping this run (default 900)")
    a.set_defaults(fn=cmd_poll)

    for name, fn, helptext in (("new", cmd_new, "list recently discovered postings"),
                               ("search", cmd_search, "keyword search stored postings"),
                               ("export", cmd_export, "dump matches to CSV/JSON")):
        a = sub.add_parser(name, help=helptext)
        if name == "search":
            a.add_argument("query")
        if name == "export":
            a.add_argument("--out", default="jobs.csv")
        a.add_argument("--within", type=parse_window, metavar="24h|48h|7d",
                       help="posted by the company within this window")
        a.add_argument("--seen-within", type=parse_window, metavar="24h|7d",
                       help="first seen by this scraper within this window")
        a.add_argument("--max-exp", type=int, metavar="N",
                       help="keep roles asking for at most N years of experience")
        a.add_argument("--strict-exp", action="store_true",
                       help="with --max-exp, drop postings that state no requirement")
        a.add_argument("--category", nargs="+", choices=CATEGORIES)
        a.add_argument("--seniority", nargs="+")
        a.add_argument("--remote", action="store_true")
        a.add_argument("--full-time", action="store_true",
                       help="exclude interns, part-time, contract and temporary roles")
        a.add_argument("--us", action="store_true",
                       help="only postings confirmed to be in the United States")
        a.add_argument("--us-or-unknown", action="store_true",
                       help="US plus postings whose location could not be resolved")
        a.add_argument("--sponsor", action="store_true",
                       help="only companies in the USCIS top-100 H-1B sponsor list")
        a.add_argument("--min-approvals", type=int, metavar="N",
                       help="only companies with at least N H-1B approvals (5 yr)")
        a.add_argument("--location")
        a.add_argument("--limit", type=int, default=50)
        a.set_defaults(fn=fn)

    a = sub.add_parser("reclassify", help="re-score stored postings after editing classify.py")
    a.add_argument("--threshold", type=float, default=1.0)
    a.set_defaults(fn=cmd_reclassify)

    a = sub.add_parser("enrich", help="fetch descriptions for matches that lack one")
    a.add_argument("--limit", type=int, default=2000)
    a.set_defaults(fn=cmd_enrich)

    a = sub.add_parser("dashboard", help="regenerate the HTML dashboard from the database")
    a.add_argument("--out", default="dashboard.html")
    a.set_defaults(fn=cmd_dashboard)

    a = sub.add_parser("stats", help="summary of what is tracked")
    a.set_defaults(fn=cmd_stats)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    conn = db.connect(args.db)
    try:
        return args.fn(args, conn) or 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
