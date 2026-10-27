"""SmartRecruiters (jobs.smartrecruiters.com/<CompanyId>). Public Posting API, no key needed.

The list endpoint has no descriptions, so details are fetched only for postings that
could be open to the Philippines (in PH, or remote). Large employers stay cheap to crawl.
"""
from __future__ import annotations

from .base import CompanySpec, Http, RawJob, parse_dt

API = "https://api.smartrecruiters.com/v1/companies/{key}/postings"
PAGE = 100
MAX_PAGES = 50


async def fetch(company: CompanySpec, http: Http) -> list[RawJob]:
    base = API.format(key=company.key)
    postings: list[dict] = []
    for page in range(MAX_PAGES):
        data = await http.get_json(base, params={"limit": PAGE, "offset": page * PAGE})
        batch = data.get("content") or []
        postings.extend(batch)
        if len(batch) < PAGE or len(postings) >= (data.get("totalFound") or 0):
            break

    jobs: list[RawJob] = []
    for p in postings:
        loc = p.get("location") or {}
        country = (loc.get("country") or "").upper()
        remote = bool(loc.get("remote"))
        if country != "PH" and not remote:
            continue
        detail = {}
        try:
            detail = await http.get_json(f"{base}/{p['id']}")
        except Exception:
            pass   # keep the posting even if its description can't be read this time
        sections = ((detail.get("jobAd") or {}).get("sections") or {})
        html = "".join(
            f"<h3>{s.get('title', '')}</h3>{s.get('text', '')}"
            for key in ("jobDescription", "qualifications", "additionalInformation")
            if (s := sections.get(key)) and s.get("text"))
        jobs.append(RawJob(
            external_id=str(p["id"]),
            title=(p.get("name") or "").strip(),
            url=detail.get("postingUrl") or f"https://jobs.smartrecruiters.com/{company.key}/{p['id']}",
            apply_url=detail.get("applyUrl"),
            location=", ".join(x for x in (loc.get("city"), loc.get("region")) if x) or None,
            country_codes=[country] if country else [],
            remote=remote,
            workplace_type="remote" if remote else ("hybrid" if loc.get("hybrid") else None),
            department=((p.get("department") or {}).get("label") or (p.get("function") or {}).get("label")),
            employment_type=(p.get("typeOfEmployment") or {}).get("label"),
            description_html=html or None,
            posted_at=parse_dt(p.get("releasedDate")),
        ))
    return jobs
