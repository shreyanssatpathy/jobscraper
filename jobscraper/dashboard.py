"""Generate a self-contained HTML dashboard from the local database."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

TEMPLATE = Path(__file__).with_name("dashboard_template.html")


def build(conn: sqlite3.Connection, out_path: str | Path) -> tuple[Path, int]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT j.id, j.title, s.company, s.ats, s.h1b_approvals, j.role_category,
               j.seniority, j.min_years_exp, j.location, j.is_remote, j.salary_min,
               j.salary_max, j.employment_type, j.posted_at, j.first_seen_at, j.url, j.is_us
        FROM job j JOIN source s ON s.id = j.source_id
        WHERE j.is_match = 1 AND j.closed_at IS NULL
        ORDER BY COALESCE(j.posted_at, j.first_seen_at) DESC""").fetchall()

    # Positional records keep the embedded payload small; the page unpacks them.
    jobs = [[r["id"], r["title"], r["company"], r["role_category"], r["seniority"],
             r["min_years_exp"], r["location"], r["is_remote"], r["salary_min"],
             r["salary_max"], r["employment_type"], (r["posted_at"] or "")[:10],
             (r["first_seen_at"] or "")[:10], r["url"], r["h1b_approvals"], r["is_us"]]
            for r in rows]

    boards = conn.execute("SELECT COUNT(*) FROM source").fetchone()[0]
    tracked = conn.execute("SELECT COUNT(*) FROM job").fetchone()[0]
    sponsors = conn.execute(
        "SELECT COUNT(*) FROM source WHERE h1b_approvals IS NOT NULL").fetchone()[0]
    # Per-tier freshness: the two tiers cost wildly different amounts to poll,
    # so the page reports them separately rather than as one "last updated".
    tiers = {}
    for key, sql in (("tier1", "ats != 'workday'"), ("workday", "ats = 'workday'")):
        row = conn.execute(
            f"SELECT COUNT(*), MAX(last_polled_at) FROM source WHERE {sql}").fetchone()
        tiers[key] = {"boards": row[0], "lastPoll": row[1]}

    meta = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "boards": boards, "tracked": tracked, "sponsors": sponsors,
        "tiers": tiers,
    }
    html = TEMPLATE.read_text()
    html = html.replace("/*__JOBS__*/[]", json.dumps(jobs, separators=(",", ":")))
    html = html.replace("/*__META__*/{}", json.dumps(meta, separators=(",", ":")))
    out = Path(out_path)
    out.write_text(html)
    return out, len(jobs)
