"""Role taxonomy: decide whether a posting is a data/AI role we care about.

Classification runs on the *title* (plus department when available) so that we
can filter before paying for detail fetches. Descriptions are only used to
refine an already-matched job.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Hard exclusions. Checked first: any hit here means "not a role we want",
# regardless of what else the title says. Flip entries off if you disagree --
# the finance analyst rules in particular are a judgement call.
# --------------------------------------------------------------------------
DENY = [
    r"data cent(?:er|re)",
    r"data entry",
    r"data annotat",
    r"\bdata protection officer\b",
    r"privacy counsel|data privacy (?:counsel|lawyer|attorney)",
    r"recruit(?:er|ing|ment)|talent acquisition|\bsourcer\b",
    r"account executive|\bsales\b|business development|\bbdr\b|\bsdr\b",
    r"customer success|customer support|technical support",
    r"\bcounsel\b|\battorney\b|paralegal",
    r"financial analyst|investment analyst|credit analyst|equity research",
    r"\bfp&a\b|treasury analyst|accounting analyst",
    r"warehouse associate|\bdriver\b|facilities|janitor|security guard",
    r"teacher|tutor|professor",
    r"(?:product|program|project) manager",
]

# --------------------------------------------------------------------------
# Category rules: (weight, pattern). Weights are additive within a category;
# the category with the highest total wins. Weight >= 1.0 on its own is a
# confident match; 0.5-ish patterns need corroboration.
# --------------------------------------------------------------------------
RULES: dict[str, list[tuple[float, str]]] = {
    "ai_engineer": [
        (1.0, r"\b(?:machine learning|ml)\s+(?:engineer|developer)"),
        (1.0, r"\bml[eo]\b|\bmlops\b"),
        (1.0, r"\bai\s+engineer|artificial intelligence engineer"),
        (1.0, r"applied\s+ai|\bai/ml\b|\bml/ai\b"),
        (1.0, r"deep learning engineer|\bnlp\s+engineer|computer vision engineer"),
        (1.0, r"\bllm\b|generative ai|\bgen\s?ai\b|foundation model"),
        (1.0, r"ml (?:platform|infrastructure|systems)|machine learning (?:platform|infrastructure)"),
        (1.0, r"\bperception engineer\b|\bprompt engineer\b"),
        (1.0, r"(?:ai|ml|machine learning|data)\s+solutions?\s+architect"),
        (0.8, r"forward[- ]deployed\s+(?:software\s+)?engineer"),
        (0.7, r"research engineer"),
        (0.6, r"member of (?:the )?technical staff"),
        (0.5, r"\bai\b|machine learning|\bml\b"),
    ],
    "data_scientist": [
        (1.0, r"data scientist|data science"),
        (1.0, r"applied scientist|research scientist"),
        (1.0, r"machine learning scientist"),
        (1.0, r"decision scientist"),
        (1.0, r"quantitative (?:researcher|analyst|scientist)|\bquant researcher"),
        (1.0, r"\bstatistician\b|biostatistic|econometric|\beconometrician\b"),
        (1.0, r"experimentation\s+(?:scientist|analyst|lead)"),
        (0.6, r"experimentation|causal inference"),
    ],
    "data_engineer": [
        (1.0, r"data engineer|data engineering"),
        (1.0, r"\betl\s+(?:developer|engineer)|data pipeline"),
        (1.0, r"data (?:platform|infrastructure|warehouse|lake)\s*(?:engineer|developer)?"),
        (1.0, r"\bdata architect\b|big data"),
        (1.0, r"data\s?ops\s+(?:engineer|developer)|\bdataops\b"),
        (1.0, r"data model(?:er|ler|ling|ing)"),
        (1.0, r"data\s+(?:solutions?|integration|migration)\s+(?:engineer|developer)"),
        (1.0, r"(?:software|backend|back[- ]end|platform)\s+engineer[,(\s-]+.{0,18}\bdata\b"),
        (1.0, r"streaming (?:data )?engineer"),
        (0.8, r"\b(?:spark|hadoop|kafka|databricks|snowflake|dbt|airflow)\b"),
        (0.8, r"database (?:engineer|developer|administrator)|\bdba\b"),
    ],
    "analytics": [
        (1.0, r"data analyst|analytics engineer"),
        (1.0, r"business intelligence|\bbi\s+(?:analyst|engineer|developer)\b"),
        (1.0, r"(?:product|growth|marketing|insights|reporting|operations)\s+analyst"),
        (1.0, r"analytics (?:manager|lead|engineer|specialist)|\banalytics\b"),
        (1.0, r"data visuali[sz]ation|dashboard (?:developer|engineer|analyst|specialist)"),
        (1.0, r"reporting (?:engineer|developer|specialist)|decision support"),
        (1.0, r"\bbi\b\s+(?:consultant|specialist|architect|lead|manager)"),
        (1.0, r"business intelligence consultant"),
        (1.0, r"data (?:&|and) insights|insights (?:manager|lead|partner|specialist)"),
        (1.0, r"marketing science|measurement (?:&|and) analytics"),
        (0.7, r"\b(?:tableau|looker|power\s?bi|sigma|mode)\b"),
        (0.5, r"business analyst"),
    ],
}

# Department/team names that corroborate a weak title match.
DEPT_BOOST = re.compile(
    r"data|analytic|machine learning|\bml\b|\bai\b|research|insight|business intelligence",
    re.I,
)

# Order is precedence: the first rule that fires wins. Manager/staff/senior are
# checked before junior so "Senior Manager" reads as manager, and roman numerals
# are handled explicitly (II is mid, III/IV senior) rather than via a \bjr\b-ish
# rule that would swallow "Applied Scientist II".
SENIORITY = [
    ("intern",   r"\bintern(?:ship)?\b|\bco-?op\b"),
    ("new_grad", r"new ?grad|university grad|campus|early career|graduate (?:program|scheme)|entry[- ]level"),
    ("manager",  r"\bmanager\b|head of|\bdirector\b|\bvp\b|vice president|\bchief\b"),
    ("staff",    r"\bstaff\b|\bprincipal\b|distinguished|\bfellow\b|\barchitect\b"),
    ("senior",   r"\bsenior\b|\bsr\.?\b|\blead\b|\biii\b|\biv\b|\bl[5-9]\b"),
    ("junior",   r"\bjunior\b|\bjr\.?\b|\bassociate\b"),
    ("mid",      r"\bii\b"),
]

REMOTE = re.compile(r"\bremote\b|work from home|\bwfh\b|distributed|anywhere", re.I)
ONSITE = re.compile(r"\bon-?site\b|\bin-?office\b", re.I)

_DENY_RE = [re.compile(p, re.I) for p in DENY]
_RULES_RE = {c: [(w, re.compile(p, re.I)) for w, p in ps] for c, ps in RULES.items()}
_SEN_RE = [(n, re.compile(p, re.I)) for n, p in SENIORITY]


@dataclass
class Match:
    matched: bool
    category: str | None = None
    score: float = 0.0
    seniority: str | None = None
    scores: dict[str, float] = field(default_factory=dict)
    terms: list[str] = field(default_factory=list)
    denied_by: str | None = None


def seniority_of(title: str) -> str:
    """First rule that fires wins, so order in SENIORITY is precedence."""
    for name, rx in _SEN_RE:
        if rx.search(title):
            return name
    return "mid"


def is_remote(*texts: str | None) -> bool | None:
    blob = " ".join(t for t in texts if t)
    if not blob:
        return None
    if REMOTE.search(blob):
        return True
    if ONSITE.search(blob):
        return False
    return None


def classify(title: str, department: str | None = None, team: str | None = None,
             threshold: float = 1.0) -> Match:
    """Score a posting against the four target role families."""
    title = (title or "").strip()
    if not title:
        return Match(False)

    for rx in _DENY_RE:
        if rx.search(title):
            return Match(False, denied_by=rx.pattern)

    context = " ".join(x for x in (department, team) if x)
    boost = 0.3 if context and DEPT_BOOST.search(context) else 0.0

    scores: dict[str, float] = {}
    terms: list[str] = []
    for category, rules in _RULES_RE.items():
        total = 0.0
        for weight, rx in rules:
            m = rx.search(title)
            if m:
                total += weight
                terms.append(m.group(0).strip().lower())
        if total:
            scores[category] = round(total + boost, 2)

    if not scores:
        return Match(False)

    category = max(scores, key=lambda k: scores[k])
    score = scores[category]
    return Match(
        matched=score >= threshold,
        category=category,
        score=score,
        seniority=seniority_of(title),
        scores=scores,
        terms=sorted(set(terms)),
    )
