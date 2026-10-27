"""We Work Remotely (weworkremotely.com). Public RSS feed, no key needed.

WWR's RSS terms should be checked before republishing; jobs link back to the
original listing and keep the employer and region the feed provides.
"""
from __future__ import annotations

import html
import xml.etree.ElementTree as ET
from datetime import timezone
from email.utils import parsedate_to_datetime

from .base import CompanySpec, Http, RawJob, SourceError

FEEDS = {
    "all": "https://weworkremotely.com/remote-jobs.rss",
    "programming": "https://weworkremotely.com/categories/remote-programming-jobs.rss",
    "design": "https://weworkremotely.com/categories/remote-design-jobs.rss",
    "customer-support": "https://weworkremotely.com/categories/remote-customer-support-jobs.rss",
    "sales-marketing": "https://weworkremotely.com/categories/remote-sales-and-marketing-jobs.rss",
    "product": "https://weworkremotely.com/categories/remote-product-jobs.rss",
}


def _text(item: ET.Element, tag: str) -> str:
    node = item.find(tag)
    return (node.text or "").strip() if node is not None else ""


def _posted(value: str):
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def fetch(company: CompanySpec, http: Http) -> list[RawJob]:
    url = FEEDS.get(str(company.options.get("feed", "all")), FEEDS["all"])
    resp = await http.get(url)
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as exc:
        raise SourceError(f"Expected RSS from {url}") from exc

    max_jobs = max(1, int(company.options.get("max_jobs", 100)))
    jobs: list[RawJob] = []
    for item in root.findall(".//item")[:max_jobs]:
        raw_title = _text(item, "title")
        employer, sep, title = raw_title.partition(": ")
        if not sep:
            employer, title = "", raw_title
        region = _text(item, "region")
        country = _text(item, "country")
        state = _text(item, "state")
        locations = [x for x in (region, country, state) if x]
        low = region.lower()
        description = _text(item, "description")
        jobs.append(RawJob(
            external_id=_text(item, "guid") or _text(item, "link"),
            title=title or raw_title,
            url=_text(item, "link"),
            apply_url=_text(item, "link"),
            company_name=employer or None,
            location=", ".join(locations) or None,
            locations=locations,
            remote=True,
            workplace_type="remote",
            department=_text(item, "category") or _text(item, "skills") or None,
            employment_type=_text(item, "type") or None,
            description_html=html.unescape(description) if description else None,
            posted_at=_posted(_text(item, "pubDate")),
            worldwide=(not locations or "anywhere in the world" in low or "worldwide" in low),
        ))
    return [job for job in jobs if job.external_id and job.title and job.url]
