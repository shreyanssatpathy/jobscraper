"""Decide whether a posting's location is in the United States.

Location strings from these boards are free text and frequently ambiguous: the
prefix in `IN - Bengaluru, India` is a country code, while the identical prefix
in `IN-INDIANAPOLIS, 220 VIRGINIA AVE` is a state code. `CA` is California and
Canada; `DE` is Delaware and Germany; `San Jose` is in both California and Costa
Rica.

Resolution order per location: an explicit non-US country or city marker wins
first, then explicit US markers, then state names, then state codes, then a
short list of unmistakable US cities. Anything left is reported as unknown
rather than guessed -- callers decide whether to keep unknowns.
"""
from __future__ import annotations

import re

STATE_NAMES = [
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana",
    "iowa", "kansas", "kentucky", "louisiana", "maine", "maryland",
    "massachusetts", "michigan", "minnesota", "mississippi", "missouri",
    "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming",
    "district of columbia",
]
STATE_CODES = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DC", "DE", "FL", "GA", "HI",
    "IA", "ID", "IL", "IN", "KS", "KY", "LA", "MA", "MD", "ME", "MI", "MN",
    "MO", "MS", "MT", "NC", "ND", "NE", "NH", "NJ", "NM", "NV", "NY", "OH",
    "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VA", "VT", "WA",
    "WI", "WV", "WY",
]

# Countries and unmistakable non-US cities seen across these boards.
NON_US = [
    "india", "canada", "united kingdom", "england", "scotland", "ireland",
    "germany", "france", "spain", "portugal", "italy", "netherlands",
    "belgium", "switzerland", "sweden", "norway", "denmark", "finland",
    "poland", "romania", "czech", "hungary", "austria", "greece",
    "singapore", "japan", "china", "hong kong", "taiwan", "korea",
    "australia", "new zealand", "israel", "brazil", "mexico", "argentina",
    "chile", "colombia", "peru", "costa rica", "uruguay", "philippines",
    "vietnam", "thailand", "malaysia", "indonesia", "pakistan", "bangladesh",
    "sri lanka", "egypt", "nigeria", "kenya", "south africa", "morocco",
    "turkey", "uae", "uk", "dubai", "abu dhabi", "saudi", "qatar", "bahrain",
    "mauritius", "latvia", "lithuania", "estonia", "ukraine", "bulgaria",
    "serbia", "croatia", "slovakia", "slovenia", "luxembourg", "iceland",
    "emirates",
    # cities that appear without their country
    "bengaluru", "bangalore", "hyderabad", "pune", "mumbai", "chennai",
    "gurgaon", "gurugram", "noida", "delhi", "kolkata", "ahmedabad",
    "london", "dublin", "berlin", "munich", "paris", "amsterdam", "madrid",
    "barcelona", "milan", "rome", "zurich", "geneva", "stockholm", "warsaw",
    "prague", "budapest", "vienna", "lisbon", "brussels", "copenhagen",
    "toronto", "vancouver", "montreal", "ottawa", "calgary", "waterloo, on",
    "tokyo", "osaka", "seoul", "shanghai", "beijing", "shenzhen", "taipei",
    "sydney", "melbourne", "auckland", "tel aviv", "haifa", "herzliya",
    "sao paulo", "são paulo", "mexico city", "guadalajara", "bogota",
    "buenos aires", "santiago", "manila", "cebu", "jakarta", "bangkok",
    "kuala lumpur", "ho chi minh", "hanoi", "cairo", "lagos", "nairobi",
    "cape town", "johannesburg", "istanbul", "riga", "vilnius", "tallinn",
    "krakow", "wroclaw", "bucharest", "sofia", "belgrade", "zagreb",
    "yokneam", "ebene", "cork", "belfast", "edinburgh", "manchester",
    "bhubaneswar", "indore", "jaipur", "coimbatore", "kochi", "trivandrum",
    "nagpur", "vadodara", "surat", "lucknow", "bhopal", "andhra pradesh",
    "islamabad", "lahore", "karachi", "dhaka", "colombo", "kathmandu",
    "cluj", "brno", "bratislava", "ljubljana", "tallinn", "penang",
    "kuala", "da nang", "hanoi", "taguig", "makati", "quezon",
    "bogota", "bogotá", "medellin", "lima", "quito", "panama city",
    "milano", "torino", "napoli", "firenze", "kronberg", "stuttgart",
    "hamburg", "frankfurt", "cologne", "köln", "dusseldorf", "düsseldorf",
    "helsinki", "oslo", "gothenburg", "malmo", "aarhus", "utrecht",
    "rotterdam", "eindhoven", "antwerp", "ghent", "basel", "lausanne",
    "mississauga", "ottawa", "halifax", "winnipeg", "edmonton", "quebec",
    "türkiye", "turkiye", "ankara", "izmir", "casablanca", "tunis",
    "\\bind\\b", "\\bpk\\b", "\\bmys\\b", "\\bcze\\b", "\\bvnm\\b",
    "\\bcol\\b", "\\bphl\\b", "\\bidn\\b", "\\bsgp\\b", "\\bgbr\\b",
    "\\bdeu\\b", "\\bfra\\b", "\\besp\\b", "\\bita\\b", "\\bnld\\b",
    "\\bbra\\b", "\\bmex\\b", "\\bjpn\\b", "\\bchn\\b", "\\bkor\\b",
    "\\baus\\b", "\\bcan\\b", "\\bisr\\b", "\\bare\\b", "\\bpol\\b",
    "cambridge, uk", "oxford, uk", "reading, uk", "leeds", "bristol, uk",
]

US_MARKERS = [
    r"\bunited states\b", r"\bu\.?s\.?a\b", r"\busa\b",
    r"(?:^|[\s,\-(])u\.s\.(?:$|[\s,\-)])",
    r"(?:^|[\s,(\-])us(?:$|[\s,)\-])", r"\bus\s*-\s*", r"^us[,\s]",
    r"\bremote\s*[-–,]\s*us\b", r"\bnationwide\b",
]

# Cities distinctive enough to imply the US on their own. Deliberately excludes
# San Jose (also Costa Rica), Cambridge, Birmingham, Columbus and similar.
US_CITIES = [
    "san francisco", "new york city", "nyc", "seattle", "bellevue", "redmond",
    "mountain view", "palo alto", "sunnyvale", "santa clara", "cupertino",
    "menlo park", "san mateo", "redwood city", "los angeles", "san diego",
    "austin", "dallas", "houston", "chicago", "boston", "cambridge, ma",
    "denver", "boulder", "atlanta", "miami", "philadelphia", "pittsburgh",
    "phoenix, az", "tempe", "portland, or", "salt lake city", "minneapolis",
    "detroit", "nashville", "charlotte", "raleigh", "durham, nc", "st. louis",
    "kansas city", "las vegas", "sacramento", "san antonio", "indianapolis",
    "milwaukee", "cleveland", "cincinnati", "washington, dc", "arlington, va",
    "mclean", "reston", "bethesda", "brooklyn", "manhattan", "hartford",
    "stamford", "princeton", "hoboken", "jersey city", "irvine", "pasadena",
    "santa monica", "culver city", "el segundo", "fremont", "san bruno",
    "south san francisco", "foster city", "emeryville", "oakland, ca",
    "sf office", "strava sf", "\\bsf\\b",
]

# Word-bounded: an unbounded "india" matches inside "INDIANAPOLIS", which
# turned an Indiana address into a rejected non-US location.
_NON_US = re.compile(
    r"\b(?:" + "|".join(t if t.startswith("\\b") else re.escape(t) for t in NON_US) + r")\b",
    re.I)
_US_MARK = re.compile("|".join(US_MARKERS), re.I)
_STATE_NAME = re.compile(r"\b(?:" + "|".join(STATE_NAMES) + r")\b", re.I)
_STATE_CODE = re.compile(
    r"(?:^|[,(\s])(" + "|".join(STATE_CODES) + r")(?:$|[,)\s]|\s*[-–]\s*|\s*\d{5})")
_US_CITY = re.compile(
    "|".join(c if c.startswith("\\b") else re.escape(c) for c in US_CITIES), re.I)

REMOTE_ONLY = re.compile(r"^\s*(?:fully\s+)?remote\s*$", re.I)


def _one(part: str) -> bool | None:
    p = part.strip()
    if not p:
        return None
    # A listing naming several countries including the US ("United States,
    # Canada, London") is a US posting, so an explicit US marker outranks a
    # non-US one inside the same location string.
    if _US_MARK.search(p):
        return True
    if _NON_US.search(p):
        return False
    if _STATE_NAME.search(p):
        return True
    if _STATE_CODE.search(p):
        return True
    if _US_CITY.search(p):
        return True
    return None


def is_us(location: str | None) -> bool | None:
    """True / False / None (unknown) for a free-text location string.

    Multi-location postings count as US when any one location is in the US.
    """
    if not location or not location.strip():
        return None
    if REMOTE_ONLY.match(location):
        return None          # "Remote" with no region says nothing about country
    parts = re.split(r"[;|]|\s{2,}", location)
    verdicts = [_one(p) for p in parts if p.strip()]
    if any(v is True for v in verdicts):
        return True
    if verdicts and all(v is False for v in verdicts):
        return False
    if any(v is False for v in verdicts):
        return False
    return None
