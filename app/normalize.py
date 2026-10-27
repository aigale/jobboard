"""Turn a raw job from any source into a clean Philippine job record.

Everything here is pure functions so it can be tested without a network or database.
The rules are keyword based on purpose: they are easy to read, easy to extend, and
good enough to sort thousands of listings into the filters the site shows.
"""
from __future__ import annotations

import hashlib
import html as htmllib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import nh3
from bs4 import BeautifulSoup

from .common import normalize_job_type
from .config import Settings, settings as default_settings
from .sources.base import RawJob, StructuredSalary

# ---------------------------------------------------------------------------
# Location
# ---------------------------------------------------------------------------

# keyword (lowercase) -> (display bucket, city name). Longest keywords are matched first.
_PLACES: dict[str, tuple[str, str]] = {}


def _add(bucket: str, *pairs: tuple[str, str] | str) -> None:
    for p in pairs:
        kw, city = (p, p.title()) if isinstance(p, str) else p
        _PLACES[kw] = (bucket, city)


_add("Metro Manila",
     ("metro manila", "Metro Manila"), ("national capital region", "Metro Manila"), ("ncr", "Metro Manila"),
     "makati", "taguig", ("bgc", "Taguig"), ("bonifacio global city", "Taguig"), ("mckinley", "Taguig"),
     "pasig", ("ortigas", "Pasig"), "quezon city", ("eastwood", "Quezon City"), ("cubao", "Quezon City"),
     "mandaluyong", "pasay", "parañaque", ("paranaque", "Parañaque"), ("alabang", "Muntinlupa"), "muntinlupa",
     ("san juan city", "San Juan"), "marikina", "caloocan", "las piñas", ("las pinas", "Las Piñas"), "valenzuela",
     ("rockwell", "Makati"), ("manila", "Manila"))
_add("Cebu", ("cebu", "Cebu City"), ("cebu city", "Cebu City"), "mandaue", ("lapu-lapu", "Lapu-Lapu"),
     ("lapu lapu", "Lapu-Lapu"), ("it park", "Cebu City"))
_add("Davao", ("davao", "Davao City"), ("davao city", "Davao City"))
_add("Iloilo", ("iloilo", "Iloilo City"), ("iloilo city", "Iloilo City"))
_add("Bacolod", "bacolod")
_add("Clark, Pampanga", ("clark", "Clark"), ("pampanga", "Pampanga"), ("angeles city", "Angeles City"),
     ("san fernando, pampanga", "San Fernando"))
_add("Baguio", ("baguio", "Baguio"))
_add("Cagayan de Oro", ("cagayan de oro", "Cagayan de Oro"), ("cdo", "Cagayan de Oro"))
_add("Laguna", ("laguna", "Laguna"), ("santa rosa, laguna", "Santa Rosa"), ("calamba", "Calamba"))
_add("Cavite", ("cavite", "Cavite"), ("imus", "Imus"), ("dasmariñas", "Dasmariñas"))
_add("Batangas", ("batangas", "Batangas"), ("lipa city", "Lipa"))
_add("Bulacan", ("bulacan", "Bulacan"), ("malolos", "Malolos"))
_add("Dumaguete", "dumaguete")
_add("General Santos", ("general santos", "General Santos"))
_add("Zamboanga", ("zamboanga", "Zamboanga City"))
_add("Tacloban", "tacloban")
_add("Bicol", ("naga city", "Naga"), ("legazpi", "Legazpi"))

_REGION_KEYS = {"metro manila", "national capital region", "ncr", "pampanga", "laguna", "cavite", "batangas", "bulacan"}
# Specific cities win over region names ("Makati City, Metro Manila" -> Makati), then longest keyword first.
_PLACE_KEYS = sorted(_PLACES, key=lambda k: (k in _REGION_KEYS, -len(k)))
_PLACE_RES = {k: re.compile(r"(?<![a-z])" + re.escape(k) + r"(?![a-z])") for k in _PLACE_KEYS}

_PH_COUNTRY_RE = re.compile(r"philippines|pilipinas|filipin", re.I)
_PH_CODE_RE = re.compile(r"(?:^|[\s,(/\-])(PH|PHL)(?:$|[\s,)/\-])")   # case-sensitive on purpose
_APAC_RE = re.compile(r"\bapac\b|asia[\s-]?pacific|south\s?east asia|southeast asia|\bsea\b|\basia\b|gmt\s?\+\s?8|utc\s?\+\s?8", re.I)
_WORLDWIDE_RE = re.compile(r"anywhere|worldwide|global|international|work from anywhere", re.I)
_REMOTE_RE = re.compile(r"remote|work from home|\bwfh\b|telecommut|home[\s-]?based|virtual", re.I)
_OTHER_COUNTRY_RE = re.compile(
    r"\b(united states|usa|u\.s\.|canada|united kingdom|\buk\b|ireland|germany|france|spain|netherlands|"
    r"poland|portugal|brazil|mexico|argentina|colombia|india|pakistan|australia|new zealand|singapore|"
    r"japan|korea|china|vietnam|indonesia|malaysia|thailand|emea|latam|americas|europe)\b", re.I)


@dataclass
class LocationInfo:
    eligibility: str | None   # ph_location | ph_remote | apac_remote | worldwide_remote | None (not PH)
    bucket: str
    city: str | None


def find_city(text: str) -> tuple[str, str] | None:
    low = text.lower()
    for key in _PLACE_KEYS:
        if _PLACE_RES[key].search(low):
            return _PLACES[key]
    return None


def classify_location(raw: RawJob, is_remote: bool) -> LocationInfo:
    locs = " | ".join([raw.location or "", *raw.locations])
    codes = {c.upper() for c in raw.country_codes if c}
    city = find_city(locs)
    ph_hit = bool(_PH_COUNTRY_RE.search(locs) or _PH_CODE_RE.search(locs) or codes & {"PH", "PHL"} or city)

    if not ph_hit and is_remote and raw.description_text and re.search(
            r"(based|located|residing|reside|living)\s+in\s+(the\s+)?philippines|philippine[- ]based|"
            r"open to (candidates|applicants) (in|from) (the\s+)?philippines", raw.description_text, re.I):
        ph_hit = True

    if ph_hit:
        eligibility = "ph_remote" if is_remote else "ph_location"
    elif is_remote and _APAC_RE.search(locs):
        eligibility = "apac_remote"
    elif is_remote and (not locs.strip(" |") or _WORLDWIDE_RE.search(locs)
                        or (re.fullmatch(r"[\s|]*remote[\s|]*", locs, re.I) and not _OTHER_COUNTRY_RE.search(locs))):
        eligibility = "worldwide_remote"
    else:
        eligibility = None

    if is_remote:
        bucket = "Remote (PH-based)"
    elif city:
        bucket = city[0]
    else:
        bucket = "Philippines (other)"
    return LocationInfo(eligibility, bucket, city[1] if city else None)


def is_allowed(eligibility: str | None, cfg: Settings) -> bool:
    if eligibility in ("ph_location", "ph_remote"):
        return True
    if eligibility == "apac_remote":
        return cfg.include_apac_remote
    if eligibility == "worldwide_remote":
        return cfg.include_worldwide_remote
    return False


# ---------------------------------------------------------------------------
# Work setup, shift, level, category, perks
# ---------------------------------------------------------------------------

def detect_setup(raw: RawJob) -> str:
    wt = (raw.workplace_type or "").lower().replace("_", "-")
    if wt in ("remote",):
        return "Remote"
    if wt in ("hybrid",):
        return "Hybrid"
    if wt in ("onsite", "on-site", "in-office", "office"):
        return "On-site"
    head = " ".join([raw.title, raw.location or "", *raw.locations]).lower()
    body = (raw.description_text or "")[:4000].lower()
    if "hybrid" in head or re.search(r"\bhybrid (set-?up|work|arrangement|schedule|role)\b", body):
        return "Hybrid"
    if raw.remote or _REMOTE_RE.search(head) or re.search(r"fully remote|100% remote|permanent work from home", body):
        return "Remote"
    return "On-site"


_ROTATING_RE = re.compile(r"rotating shift|rotational shift|shifting schedule|willing to work on shifting|\b24/7\b", re.I)
_NIGHT_RE = re.compile(
    r"night shift|nightshift|graveyard|night differential|\bus (business )?hours\b|\bus time ?zones?\b|"
    r"(?<!asia[- ])(?<!asia-)\b(eastern|pacific|central|mountain) (standard |daylight )?time\b(?! zones? \(?apac)|"
    r"\b(est|pst|cst|edt|pdt|et|pt) (hours|time ?zone|schedule)|\bwork (est|pst|cst|edt|pdt) hours|"
    r"aligned (with|to) (the )?us|north american (business )?hours", re.I)
_DAY_RE = re.compile(r"day ?shift|\bdayshift\b|(8|9) ?am ?(to|-|–) ?(5|6) ?pm|philippine (business )?hours|manila time", re.I)


def detect_shift(raw: RawJob) -> str:
    text = f"{raw.title}\n{raw.description_text or ''}"
    if _ROTATING_RE.search(text):
        return "Rotating"
    if _NIGHT_RE.search(text):
        return "Night shift"
    if _DAY_RE.search(text):
        return "Day shift"
    return "Not stated"


_SENIOR_RE = re.compile(r"\b(senior|sr\.?|lead|principal|head|director|vp|vice president|chief|architect)\b|"
                        r"\bstaff (software |platform |data |product |site reliability )?(engineer|developer|designer|scientist)", re.I)
_MANAGER_RE = re.compile(r"(?<!account )(?<!community )(?<!social media )(?<!property )(?<!case )(?<!office )\bmanager\b", re.I)
_ENTRY_RE = re.compile(r"\b(junior|jr\.?|entry[\s-]?level|graduate|fresh grad\w*|intern(ship)?|trainee|apprentice|level 1|\bi)\s*$|"
                       r"\b(junior|jr\.?|entry[\s-]?level|graduate|intern(ship)?|trainee|apprentice)\b", re.I)
_YEARS_RE = re.compile(r"(\d{1,2})\s*\+?\s*(?:[-–to]+\s*\d{1,2}\s*)?(?:years?|yrs?)\b[^.\n]{0,25}experience", re.I)


def detect_level(raw: RawJob) -> str:
    t = raw.title
    if _ENTRY_RE.search(t):
        return "Entry level"
    if _SENIOR_RE.search(t) or _MANAGER_RE.search(t):
        return "Senior"
    body = raw.description_text or ""
    if re.search(r"fresh grad|entry[\s-]level|no (prior )?experience (required|needed|necessary)|career shifters? (are )?welcome", body, re.I):
        return "Entry level"
    m = _YEARS_RE.search(body)
    if m:
        years = int(m.group(1))
        if years >= 5:
            return "Senior"
        if years <= 1:
            return "Entry level"
    return "Mid level"


_CATEGORIES: list[tuple[str, re.Pattern]] = [(name, re.compile(rx, re.I)) for name, rx in [
    ("Virtual Assistant", r"virtual assistant|\bva\b|executive assistant|personal assistant"),
    ("Healthcare", r"nurse|nursing|medical|clinical|pharmac|physician|therap|dental|caregiver|health ?care|radiolog|\brn\b"),
    ("Customer Support", r"customer|support|call cent|\bcsr\b|chat agent|helpdesk|help desk|client (care|service)|contact cent"),
    ("Data", r"\bdata\b|analytics|machine learning|\bml\b|\bai\b engineer|\bbi\b|business intelligence|data scien"),
    ("Design", r"design|\bux\b|\bui\b|graphic|creative|illustrat|video editor|animator|motion"),
    ("Marketing", r"marketing|\bseo\b|content|social media|copywrit|brand|growth|communications|\bpr\b|editor"),
    ("Sales", r"sales|account executive|business development|\bsdr\b|\bbdr\b|account manager|real estate agent|partnerships"),
    ("Finance", r"accountant|accounting|finance|financial|bookkeep|audit|\btax\b|payroll|treasury|billing|collections|credit"),
    ("HR & Recruiting", r"recruit|talent|\bhr\b|human resources|people (ops|operations|partner)|sourcer"),
    ("Legal", r"legal|lawyer|paralegal|compliance|attorney|counsel"),
    ("Education", r"teacher|tutor|instructor|\besl\b|education|professor|faculty"),
    ("Engineering", r"engineer|developer|software|devops|\bqa\b|quality assurance|\bsre\b|programmer|backend|front ?end|"
                    r"full ?stack|mobile|ios|android|sysadmin|system administrator|network|security|cloud|\bit\b"),
    ("Operations", r"operations|logistics|supply chain|warehouse|procurement|project manager|program manager|"
                   r"product manager|admin|office|coordinator|dispatch"),
]]


def detect_category(raw: RawJob) -> str:
    for text in (raw.title, raw.department or ""):
        for name, rx in _CATEGORIES:
            if text and rx.search(text):
                return name
    return "Other"


_PERKS: list[tuple[str, re.Pattern]] = [(name, re.compile(rx, re.I)) for name, rx in [
    ("13th month", r"13th[\s-]month"),
    ("Night diff", r"night (shift )?diff"),
    ("Paid leave", r"paid (time off|leaves?|vacation)|\bpto\b|vacation leaves?|sick leaves?|\bvl\b.{0,10}\bsl\b"),
    ("Rice allowance", r"rice (allowance|subsidy)"),
    ("Internet allowance", r"internet (allowance|reimbursement|subsidy)"),
    ("Equipment provided", r"laptop (is |will be )?provided|company[- ]issued (laptop|equipment)|equipment (is )?provided"),
    ("Equity", r"stock options|\bequity\b|\besop\b|\brsus?\b"),
    ("Commission", r"commission"),
    ("Bonus", r"performance bonus|annual bonus|incentives?"),
    ("Free shuttle", r"shuttle"),
    ("Free meals", r"free (meals|lunch|food)"),
    ("Learning budget", r"(learning|training|education(al)?) (budget|allowance|stipend)"),
]]
_HMO_RE = re.compile(r"\bhmo\b|health (insurance|card)|medical insurance|healthcare coverage|maxicare|intellicare|medicard", re.I)
_DAY1_RE = re.compile(r"(day[\s-]?(1|one)|first day|upon hiring|from (the )?start)", re.I)


def detect_perks(raw: RawJob) -> list[str]:
    text = raw.description_text or ""
    perks: list[str] = []
    m = _HMO_RE.search(text)
    if m:
        window = text[max(0, m.start() - 80): m.end() + 80]
        perks.append("HMO day 1" if _DAY1_RE.search(window) else "HMO")
    perks += [name for name, rx in _PERKS if rx.search(text)]
    return perks


# ---------------------------------------------------------------------------
# Salary
# ---------------------------------------------------------------------------

_CUR = r"₱|PHP|Php|php|P(?=\d)|US\$|USD|AUD|A\$|SGD|S\$|EUR|€|GBP|£|\$"
_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_SAL_RE = re.compile(
    rf"(?P<cur>{_CUR})\s?(?P<a>{_NUM})\s*(?P<ak>[kK])?"
    rf"(?:\s*(?:-|–|—|to)\s*(?:(?:{_CUR})\s?)?(?P<b>{_NUM})\s*(?P<bk>[kK])?)?"
    rf"(?![\d.,]*\s*(?:[mMbB]\b|million|billion))"
    rf"(?P<tail>[^.\n]{{0,40}})")
_SAL_CONTEXT_RE = re.compile(r"salary|compensation|\bpay\b|rate|range|earn|budget|offer|package|per (month|hour|year)", re.I)
_CUR_MAP = {"₱": "PHP", "PHP": "PHP", "P": "PHP", "US$": "USD", "USD": "USD", "$": "USD", "AUD": "AUD",
            "A$": "AUD", "SGD": "SGD", "S$": "SGD", "EUR": "EUR", "€": "EUR", "GBP": "GBP", "£": "GBP"}
_TO_MONTH = {"YEAR": 1 / 12, "MONTH": 1.0, "WEEK": 52 / 12, "DAY": 22.0, "HOUR": 176.0}


def _interval_from(text: str) -> str | None:
    t = text.lower()
    if re.search(r"per hour|/ ?h(ou)?r\b|hourly|an hour", t):
        return "HOUR"
    if re.search(r"per day|/ ?day|daily", t):
        return "DAY"
    if re.search(r"per week|/ ?w(ee)?k|weekly", t):
        return "WEEK"
    if re.search(r"per month|/ ?mo(nth)?\b|monthly|a month", t):
        return "MONTH"
    if re.search(r"per (year|annum)|/ ?y(ea)?r|annual|yearly|a year|\bpa\b", t):
        return "YEAR"
    return None


def _guess_interval(currency: str, amount: float) -> str:
    php = amount * (1 if currency == "PHP" else 58)
    if php < 3_000:
        return "HOUR"
    if php < 600_000:
        return "MONTH"
    return "YEAR"


def parse_salary_text(text: str) -> StructuredSalary | None:
    if not text:
        return None
    for m in _SAL_RE.finditer(text):
        cur = _CUR_MAP.get(m.group("cur"), m.group("cur").upper())
        before = text[max(0, m.start() - 70): m.start()]
        if cur != "PHP" and not _SAL_CONTEXT_RE.search(before + m.group("tail")):
            continue
        a = float(m.group("a").replace(",", "")) * (1000 if m.group("ak") else 1)
        b = m.group("b")
        bval = float(b.replace(",", "")) * (1000 if (m.group("bk") or m.group("ak")) else 1) if b else None
        if a <= 0:
            continue
        interval = _interval_from(m.group("tail")) or _interval_from(before[-30:]) or _guess_interval(cur, max(a, bval or 0))
        return StructuredSalary(currency=cur, min=a, max=bval or a, interval=interval)
    return None


def salary_to_php_month(s: StructuredSalary, cfg: Settings) -> tuple[int | None, int | None, str]:
    original = _format_original(s)
    fx = cfg.fx(s.currency)
    factor = _TO_MONTH.get((s.interval or "").upper())
    if fx is None or factor is None:
        return None, None, original
    lo = s.min if s.min else s.max
    hi = s.max if s.max else s.min
    if lo is None:
        return None, None, original
    lo_php, hi_php = round(lo * factor * fx), round(hi * factor * fx)
    if lo_php > hi_php:
        lo_php, hi_php = hi_php, lo_php
    if not (5_000 <= hi_php <= 3_000_000):      # outside any plausible monthly pay: ignore
        return None, None, original
    # round to the nearest 500 pesos for display
    return int(round(lo_php / 500) * 500), int(round(hi_php / 500) * 500), original


def _format_original(s: StructuredSalary) -> str:
    def f(x):
        return f"{x:,.0f}" if x and x >= 100 else (f"{x:,.2f}" if x else "")
    rng = f(s.min) if (s.max in (None, s.min)) else f"{f(s.min)}–{f(s.max)}"
    per = {"HOUR": "hour", "DAY": "day", "WEEK": "week", "MONTH": "month", "YEAR": "year"}.get((s.interval or "").upper(), "")
    return f"{s.currency} {rng}" + (f" / {per}" if per else "")


# ---------------------------------------------------------------------------
# Description
# ---------------------------------------------------------------------------

ALLOWED_TAGS = {"p", "br", "ul", "ol", "li", "strong", "b", "em", "i", "h2", "h3", "h4", "a", "blockquote"}


def clean_description(raw_html: str | None, raw_text: str | None) -> tuple[str | None, str | None]:
    if raw_html:
        if "&lt;" in raw_html and "<" not in raw_html:   # Greenhouse returns HTML-escaped content
            raw_html = htmllib.unescape(raw_html)
        safe = nh3.clean(raw_html, tags=ALLOWED_TAGS, attributes={"a": {"href"}}, link_rel="noopener nofollow")
        text = raw_text or BeautifulSoup(safe, "lxml").get_text("\n", strip=True)
        return safe, text
    if raw_text:
        paras = "".join(f"<p>{htmllib.escape(p.strip())}</p>" for p in re.split(r"\n\s*\n", raw_text) if p.strip())
        return paras, raw_text
    return None, None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

@dataclass
class NormalizedJob:
    external_id: str
    title: str
    url: str
    apply_url: str | None
    department: str | None
    employment_type: str | None
    job_type: str
    location_raw: str
    location: str
    city: str | None
    ph_eligibility: str
    setup: str
    shift: str
    level: str
    category: str
    perks: list[str] = field(default_factory=list)
    salary_min: int | None = None
    salary_max: int | None = None
    salary_original: str | None = None
    description_html: str | None = None
    description_text: str | None = None
    posted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    content_hash: str = ""


def normalize(raw: RawJob, cfg: Settings | None = None) -> NormalizedJob | None:
    """Return a NormalizedJob, or None when the job is not open to people in the Philippines."""
    cfg = cfg or default_settings
    desc_html, desc_text = clean_description(raw.description_html, raw.description_text)
    raw.description_text = desc_text

    setup = detect_setup(raw)
    loc = classify_location(raw, is_remote=(setup == "Remote"))
    if not is_allowed(loc.eligibility, cfg):
        return None

    s = raw.salary or parse_salary_text(desc_text or "")
    smin = smax = None
    sorig = None
    if s:
        smin, smax, sorig = salary_to_php_month(s, cfg)

    location_raw = " / ".join(dict.fromkeys(x for x in [raw.location, *raw.locations] if x)) or ("Remote" if setup == "Remote" else "")
    posted = raw.posted_at or datetime.now(timezone.utc)
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=timezone.utc)

    job = NormalizedJob(
        external_id=str(raw.external_id), title=" ".join(raw.title.split()), url=raw.url, apply_url=raw.apply_url,
        department=raw.department, employment_type=raw.employment_type,
        job_type=normalize_job_type(raw.employment_type, raw.title), location_raw=location_raw[:500],
        location=loc.bucket, city=loc.city, ph_eligibility=loc.eligibility or "", setup=setup,
        shift=detect_shift(raw), level=detect_level(raw), category=detect_category(raw), perks=detect_perks(raw),
        salary_min=smin, salary_max=smax, salary_original=sorig,
        description_html=desc_html, description_text=desc_text, posted_at=posted,
    )
    job.content_hash = hashlib.sha256("\x1f".join(str(x) for x in (
        job.title, job.url, job.location_raw, job.setup, job.salary_original, desc_text or "")).encode()).hexdigest()
    return job
