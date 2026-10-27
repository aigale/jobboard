"""Any company careers page that publishes schema.org JobPosting data.

Most Philippine company sites and many local ATS products (and every page meant to show up
in Google for Jobs) embed <script type="application/ld+json"> with a JobPosting. This source:

1. Loads the careers page (respecting robots.txt).
2. Uses any JobPosting objects on that page.
3. Otherwise follows links matching `options.job_link_pattern` (a regex) and reads each job page.

companies.yaml example:
  - name: Example Corp
    source: jsonld
    key: https://careers.example.com.ph/openings
    options:
      job_link_pattern: "/openings/[a-z0-9-]+$"
      max_pages: 60
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable
from urllib.parse import urljoin, urldefrag

from bs4 import BeautifulSoup

from .base import CompanySpec, Http, RawJob, SourceError, StructuredSalary, parse_dt

_UNIT = {"HOUR": "HOUR", "DAY": "DAY", "WEEK": "WEEK", "MONTH": "MONTH", "YEAR": "YEAR"}


def _walk(node: Any) -> Iterable[dict]:
    if isinstance(node, list):
        for n in node:
            yield from _walk(n)
    elif isinstance(node, dict):
        types = node.get("@type")
        types = types if isinstance(types, list) else [types]
        if "JobPosting" in types:
            yield node
        for key in ("@graph", "itemListElement", "item", "mainEntity"):
            if key in node:
                yield from _walk(node[key])


def extract_postings(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    found: list[dict] = []
    for tag in soup.find_all("script", attrs={"type": re.compile(r"application/ld\+json", re.I)}):
        raw = (tag.string or tag.get_text() or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            try:   # some sites leave trailing commas or control characters
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", raw.replace("\n", " ")))
            except json.JSONDecodeError:
                continue
        found.extend(_walk(data))
    return found


def _text(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, dict):
        return v.get("name") or v.get("@id")
    if isinstance(v, list):
        return ", ".join(t for t in (_text(x) for x in v) if t) or None
    return str(v)


def _addresses(jp: dict) -> tuple[list[str], list[str]]:
    locs, codes = [], []
    places = jp.get("jobLocation") or []
    for place in places if isinstance(places, list) else [places]:
        addr = place.get("address") if isinstance(place, dict) else place
        if isinstance(addr, str):
            locs.append(addr)
            continue
        if not isinstance(addr, dict):
            continue
        country = _text(addr.get("addressCountry"))
        parts = [_text(addr.get(k)) for k in ("addressLocality", "addressRegion")] + [country]
        locs.append(", ".join(p for p in parts if p))
        if country and len(country) in (2, 3):
            codes.append(country)
    reqs = jp.get("applicantLocationRequirements") or []
    for r in reqs if isinstance(reqs, list) else [reqs]:
        name = _text(r)
        if name:
            locs.append(name)
    return [l for l in locs if l], codes


def _salary(jp: dict) -> StructuredSalary | None:
    bs = jp.get("baseSalary") or jp.get("estimatedSalary")
    if isinstance(bs, list):
        bs = bs[0] if bs else None
    if not isinstance(bs, dict):
        return None
    val = bs.get("value") or {}
    if isinstance(val, (int, float, str)):
        val = {"value": val}
    lo = val.get("minValue", val.get("value"))
    hi = val.get("maxValue", val.get("value"))
    try:
        lo = float(lo) if lo not in (None, "") else None
        hi = float(hi) if hi not in (None, "") else None
    except (TypeError, ValueError):
        return None
    cur = bs.get("currency") or val.get("currency")
    if not cur or (lo is None and hi is None):
        return None
    unit = _UNIT.get(str(val.get("unitText") or bs.get("unitText") or "").upper())
    return StructuredSalary(cur, lo, hi, unit)


def to_raw(jp: dict, page_url: str) -> RawJob:
    locs, codes = _addresses(jp)
    ident = jp.get("identifier")
    ident = ident.get("value") if isinstance(ident, dict) else ident
    url = jp.get("url") or page_url
    emp = jp.get("employmentType")
    return RawJob(
        external_id=str(ident or urldefrag(url)[0]),
        title=(_text(jp.get("title")) or "").strip(),
        url=url,
        apply_url=url,
        location=locs[0] if locs else None,
        locations=locs[1:],
        country_codes=codes,
        remote=str(jp.get("jobLocationType", "")).upper() == "TELECOMMUTE" or None,
        workplace_type="remote" if str(jp.get("jobLocationType", "")).upper() == "TELECOMMUTE" else None,
        department=_text(jp.get("occupationalCategory")) if isinstance(jp.get("occupationalCategory"), str) else None,
        employment_type=", ".join(emp) if isinstance(emp, list) else emp,
        description_html=jp.get("description"),
        posted_at=parse_dt(jp.get("datePosted")),
        salary=_salary(jp),
    )


async def fetch(company: CompanySpec, http: Http) -> list[RawJob]:
    listing_url = company.options.get("listing_url") or company.key
    resp = await http.get(listing_url, check_robots=True)
    postings = [(p, str(resp.url)) for p in extract_postings(resp.text)]

    pattern = company.options.get("job_link_pattern")
    if not postings and pattern:
        rx = re.compile(pattern)
        soup = BeautifulSoup(resp.text, "lxml")
        links: list[str] = []
        for a in soup.find_all("a", href=True):
            href = urldefrag(urljoin(str(resp.url), a["href"]))[0]
            if rx.search(href) and href not in links:
                links.append(href)
        for link in links[: int(company.options.get("max_pages", 50))]:
            try:
                page = await http.get(link, check_robots=True)
            except SourceError:
                continue
            postings += [(p, str(page.url)) for p in extract_postings(page.text)]

    if not postings and not pattern:
        raise SourceError(
            f"No JobPosting data found on {listing_url}. Add options.job_link_pattern so the crawler can open each job page.")
    jobs = [to_raw(p, url) for p, url in postings]
    return [j for j in jobs if j.title]
