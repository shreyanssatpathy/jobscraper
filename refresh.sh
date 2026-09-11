#!/bin/bash
# Scheduled refresh. Usage: refresh.sh tier1 | workday
#
# Both tiers take the same lock (see jobscraper/lock.py), so an overlapping run
# waits rather than colliding on SQLite. Tier 1 is cheap (~30s, one request per
# board, ETag short-circuits); Workday is not (~11 min, 20 rows per request,
# no caching), which is why they run on different schedules.
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${JOBSCRAPER_PYTHON:-/opt/anaconda3/bin/python3}"
cd "$DIR" || exit 1

TIER1_ATS=(greenhouse ashby lever smartrecruiters recruitee rippling breezy bamboohr)

case "${1:-}" in
  tier1)   ARGS=(--only "${TIER1_ATS[@]}") ;;
  workday) ARGS=(--only workday) ;;
  all)     ARGS=() ;;
  *) echo "usage: $0 tier1|workday|all" >&2; exit 2 ;;
esac

stamp() { date -u "+%Y-%m-%dT%H:%M:%SZ"; }
echo "[$(stamp)] === $1 refresh starting ==="
START=$(date +%s)

"$PY" -m jobscraper.cli poll "${ARGS[@]}"
RC=$?
if [ $RC -ne 0 ]; then
  echo "[$(stamp)] poll failed (exit $RC) — dashboard left untouched"
  exit $RC
fi

"$PY" -m jobscraper.cli dashboard --out dashboard.html
echo "[$(stamp)] === $1 refresh done in $(( $(date +%s) - START ))s ==="
