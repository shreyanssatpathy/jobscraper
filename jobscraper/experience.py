"""Pull a stated minimum years-of-experience out of a job description.

No ATS in the Tier 1 set exposes years-of-experience as a structured field
(SmartRecruiters and Recruitee carry a coarse seniority label and nothing more),
so this reads the requirement the posting states in prose. It reports only what
the text actually says -- when a posting states no number, the answer is None
rather than a guess.

Measured against 949 live postings: a number is found in ~75% of them.
"""
from __future__ import annotations

import re

# Ordered by how explicit the phrasing is. Every pattern captures a year count
# in group 1; the range pattern's group 1 is the low end ("3-5 years" -> 3).
PATTERNS = [
    r"(?:minimum|at least|min\.?|no less than)\s*(?:of\s*)?(\d{1,2})\s*(?:\+\s*)?(?:years|yrs)",
    r"(\d{1,2})\s*(?:-|to|–|—)\s*\d{1,2}\s*(?:\+\s*)?(?:years|yrs)",
    r"(\d{1,2})\s*\+\s*(?:years|yrs)",
    r"(\d{1,2})\s*(?:years|yrs)(?:\s+of)?\s+(?:relevant\s+|professional\s+|industry\s+|related\s+|work\s+|hands[- ]on\s+)?experience",
]
_RE = [re.compile(p, re.I) for p in PATTERNS]

# Sentences about the company, not the candidate, that would otherwise yield a
# bogus number ("serving customers for 20 years").
_NOISE = re.compile(
    r"(?:founded|established|in business|serving|operating|history)\s[^.]{0,40}$", re.I)

MAX_PLAUSIBLE = 20


def extract_min_years(text: str | None) -> int | None:
    """Lowest plausible years-of-experience requirement stated in the text.

    Takes the minimum across all matches: a posting that says "5+ years in
    software, 3+ years with Spark" is reachable at 3.
    """
    if not text:
        return None
    best: int | None = None
    for rx in _RE:
        for m in rx.finditer(text):
            if _NOISE.search(text[max(0, m.start() - 60):m.start()]):
                continue
            try:
                value = int(m.group(1))
            except (TypeError, ValueError):
                continue
            if 0 < value <= MAX_PLAUSIBLE:
                best = value if best is None else min(best, value)
    return best
