"""Arbeitnow job board (arbeitnow.com). Public JSON API, no key needed.

Most listings are on-site jobs in Germany; the Philippine filter keeps remote roles
that are explicitly open anywhere. Use `options: {max_pages: N}` to look deeper.
"""
from __future__ import annotations

from .base import CompanySpec, Http, RawJob, parse_dt

API = "https://www.arbeitnow.com/api/job-board-api"


async def fetch(company: CompanySpec, http: Http) -> list[RawJob]:
    max_pages = max(1, int(company.options.get("max_pages", 2)))
    url: str | None = API
    jobs: list[RawJob] = []
    seen: set[str] = set()

    for _ in range(max_pages):
        if not url:
            break
        data = await http.get_json(url)
        for j in data.get("data") or []:
            if not j.get("remote"):
                continue
            location = (j.get("location") or "").strip()
            tags = [str(x) for x in (j.get("tags") or []) if x]
            types = [str(x) for x in (j.get("job_types") or []) if x]
            low = location.lower()
            external_id = str(j.get("slug") or j.get("url") or "")
            if not external_id or external_id in seen:
                continue
            seen.add(external_id)
            jobs.append(RawJob(
                external_id=external_id,
                title=j.get("title") or "",
                url=j.get("url") or "",
                apply_url=j.get("url"),
                company_name=(j.get("company_name") or "").strip() or None,
                location=location or None,
                locations=[location] if location else [],
                remote=True,
                workplace_type="remote",
                department=", ".join(tags) or None,
                employment_type=types[0] if types else None,
                description_html=j.get("description"),
                posted_at=parse_dt(j.get("created_at")),
                worldwide=(not location or "anywhere" in low or "worldwide" in low),
            ))
        url = (data.get("links") or {}).get("next")
        if not url or len(seen) >= 300:
            break
    return [job for job in jobs if job.title and job.url]
