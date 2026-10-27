"""Search-engine pages: one page per job, category and location pages, sitemap.xml and robots.txt.

Pages are rendered on the server so Google and other search engines can read them without JavaScript.
Jobs posted on Sinag carry JobPosting structured data for Google for Jobs. Jobs crawled from other
companies' sites get a page too, but marked noindex: the company's own page is the original.
"""
from __future__ import annotations

import html
import json
from datetime import timedelta

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from .common import CATEGORIES, LOCATIONS, is_featured, job_path, public_job_filter, slug
from .config import settings
from .db import get_session
from .models import Company, Job, aware, utcnow

router = APIRouter(tags=["search engine pages"], include_in_schema=False)
E = html.escape
CAT_BY_SLUG = {slug(c): c for c in CATEGORIES}
LOC_BY_SLUG = {slug(l): l for l in LOCATIONS}
EMPLOYMENT = {"Full-time": "FULL_TIME", "Part-time": "PART_TIME", "Contract": "CONTRACTOR", "Freelance": "CONTRACTOR",
              "Internship": "INTERN"}
PAGE = 50


def _page(title: str, description: str, canonical: str, body: str, *, noindex: bool = False, ld: list | None = None,
          status: int = 200) -> HTMLResponse:
    def _ld(x):   # "</" inside JSON would end the script tag early
        return json.dumps(x, ensure_ascii=False).replace("</", "<\\/")
    lds = "".join(f'<script type="application/ld+json">{_ld(x)}</script>' for x in (ld or []))
    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{E(title)}</title>
<meta name="description" content="{E(description[:300])}">
<link rel="canonical" href="{E(canonical)}">
{'<meta name="robots" content="noindex, follow">' if noindex else ''}
<meta property="og:type" content="website"><meta property="og:title" content="{E(title)}">
<meta property="og:description" content="{E(description[:300])}"><meta property="og:url" content="{E(canonical)}">
<meta property="og:site_name" content="Sinag Jobs">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,600;12..96,800&family=Figtree:wght@400;500;600;700&family=IBM+Plex+Mono:wght@500&display=swap">
<link rel="stylesheet" href="/site.css"><link rel="icon" href="/icon-192.png"><link rel="manifest" href="/manifest.json">
<meta name="theme-color" content="#0038A8"><script src="/theme.js"></script>{lds}
</head><body>
<script src="/common.js"></script>
<div class="tricolor"></div>
<header class="top"><div class="wrap">
<a class="brand" href="/" aria-label="Sinag Jobs home"><svg viewBox="-60 -60 120 120" aria-hidden="true"><g fill="#FCD116"><use href="#ray"/><use href="#ray" transform="rotate(45)"/><use href="#ray" transform="rotate(90)"/><use href="#ray" transform="rotate(135)"/><use href="#ray" transform="rotate(180)"/><use href="#ray" transform="rotate(225)"/><use href="#ray" transform="rotate(270)"/><use href="#ray" transform="rotate(315)"/><circle r="18"/></g></svg>Sinag<b>Jobs</b></a>
<nav class="links"><a href="/jobs" class="hide-sm">All job categories</a><span class="account" id="account"></span></nav>
</div></header>
<main class="wrap page">{body}</main>
<script>S.renderAccount(document.getElementById("account"));
document.querySelectorAll("[data-track-apply]").forEach(a => a.addEventListener("click", () => {{
  try {{ navigator.sendBeacon("/api/track", new Blob([JSON.stringify({{type: "apply_click", job_id: +a.dataset.trackApply}})], {{type: "application/json"}})); }} catch (e) {{}}
}}));</script>
</body></html>"""
    return HTMLResponse(doc, status_code=status, headers={"Cache-Control": "public, max-age=300"})


def _abs(path: str) -> str:
    return settings.public_url + path


def _pay(j: Job) -> str:
    return f"₱{j.salary_min:,}–₱{j.salary_max:,} a month" if j.salary_max else "Salary not stated"


def job_posting_ld(j: Job) -> dict:
    c = j.company
    now = utcnow()
    valid = aware(j.expires_at) if j.expires_at else aware(j.posted_at) + timedelta(days=30)
    ld = {"@context": "https://schema.org", "@type": "JobPosting", "title": j.title,
          "description": j.description_html or E(j.description_text or j.title),
          "identifier": {"@type": "PropertyValue", "name": c.name, "value": str(j.id)},
          "datePosted": aware(j.posted_at).date().isoformat(), "validThrough": valid.isoformat(),
          "hiringOrganization": {"@type": "Organization", "name": c.name,
                                 **({"sameAs": c.website} if c.website else {}),
                                 **({"logo": _abs(c.logo_url) if c.logo_url.startswith("/") else c.logo_url} if c.logo_url else {})},
          "url": _abs(job_path(j)), "directApply": bool(j.easy_apply)}
    if j.job_type in EMPLOYMENT:
        ld["employmentType"] = EMPLOYMENT[j.job_type]
    if j.setup == "Remote":
        ld["jobLocationType"] = "TELECOMMUTE"
        ld["applicantLocationRequirements"] = {"@type": "Country", "name": "Philippines"}
    else:
        ld["jobLocation"] = {"@type": "Place", "address": {"@type": "PostalAddress", "addressCountry": "PH",
                                                           "addressLocality": j.city or j.location,
                                                           "addressRegion": j.location}}
    if j.salary_max:
        ld["baseSalary"] = {"@type": "MonetaryAmount", "currency": "PHP",
                            "value": {"@type": "QuantitativeValue", "minValue": j.salary_min, "maxValue": j.salary_max,
                                      "unitText": "MONTH"}}
    if j.skill_list:
        ld["skills"] = ", ".join(j.skill_list)
    if valid < now:
        ld.pop("validThrough")
    return ld


def _crumbs(items: list[tuple[str, str]]) -> dict:
    return {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": i + 1, "name": n, "item": _abs(p)} for i, (n, p) in enumerate(items)]}


def _crumb_html(items: list[tuple[str, str]]) -> str:
    return ('<nav aria-label="Breadcrumb" class="hint" style="margin-bottom:12px">'
            + " › ".join(f'<a href="{E(p)}">{E(n)}</a>' for n, p in items) + "</nav>")


@router.get("/jobs/{job_id:int}")
@router.get("/jobs/{job_id:int}/{title_slug}")
def job_page(job_id: int, request: Request, title_slug: str | None = None, db: Session = Depends(get_session)):
    j = db.scalar(select(Job).options(joinedload(Job.company)).join(Company).where(Job.id == job_id, public_job_filter()))
    if j is None:
        return _page("Job not found · Sinag Jobs", "This job is no longer open.", _abs("/jobs"),
                     '<h1 style="font-size:32px">This job is no longer open</h1><p>It may have been filled or closed. '
                     '<a href="/">See the latest jobs</a>.</p>', noindex=True, status=404)
    if title_slug != slug(j.title):
        return RedirectResponse(job_path(j), status_code=301)
    hosted = j.origin == "direct"
    cat_path, loc_path = f"/jobs/c/{slug(j.category)}", f"/jobs/l/{slug(j.location)}"
    crumbs = [("Jobs", "/jobs"), (j.category, cat_path), (j.title, job_path(j))]
    apply = (f'<a class="btn btn-red" href="/#job-{j.id}">Easy Apply</a>' if hosted and j.easy_apply else
             f'<a class="btn btn-red" href="{E(j.apply_url or j.url)}" target="_blank" rel="noopener nofollow" '
             f'data-track-apply="{j.id}">Apply on {"the company" if not hosted else E(j.company.name)} site</a>')
    facts = [("Monthly pay", _pay(j).replace(" a month", "")), ("Work setup", j.setup), ("Job type", j.job_type),
             ("Schedule", j.shift), ("Experience", j.level), ("Posted", aware(j.posted_at).strftime("%b %d, %Y"))]
    body = f"""{_crumb_html(crumbs)}
<article style="max-width:820px">
  <p class="eyebrow">{E(j.category)} · {E(j.location)}{' · Featured' if is_featured(j) else ''}</p>
  <h1 style="font-size:clamp(28px,4vw,42px);margin:6px 0 6px">{E(j.title)}</h1>
  <p style="font-size:18px;margin:0 0 16px"><b>{E(j.company.name)}</b>{(' · <a href="/company.html?c=' + E(j.company.slug) + '">Company page</a>') if hosted else ''}</p>
  <div class="facts" style="margin-bottom:18px">{"".join(f"<div><small>{E(k)}</small><b>{E(v)}</b></div>" for k, v in facts)}</div>
  <div class="actions" style="margin:0 0 24px">{apply}<a class="btn btn-ghost" href="/">Browse more jobs</a></div>
  {f'<h2 style="font-size:20px">Skills</h2><p>{E(", ".join(j.skill_list))}</p>' if j.skill_list else ''}
  {f'<h2 style="font-size:20px">Benefits</h2><p>{E(", ".join(j.perk_list))}</p>' if j.perk_list else ''}
  <h2 style="font-size:20px">About the role</h2>
  <div class="desc prose">{j.description_html or "<p>See the full description on the company's site.</p>"}</div>
  {f'<h2 style="font-size:20px">About {E(j.company.name)}</h2><p class="prose" style="white-space:pre-wrap">{E(j.company.about)}</p>' if hosted and j.company.about else ''}
  <div class="actions" style="margin-top:24px">{apply}</div>
  <p class="hint" style="margin-top:28px">More: <a href="{cat_path}">{E(j.category)} jobs</a> · <a href="{loc_path}">Jobs in {E(j.location)}</a> · <a href="/jobs/c/{slug(j.category)}/l/{slug(j.location)}">{E(j.category)} jobs in {E(j.location)}</a></p>
</article>"""
    desc = f"{j.title} at {j.company.name}, {j.location}. {_pay(j)}. {j.setup}, {j.job_type}. Apply on Sinag Jobs."
    ld = [_crumbs(crumbs)] + ([job_posting_ld(j)] if hosted else [])
    canonical = _abs(job_path(j)) if hosted else (j.url or _abs(job_path(j)))
    return _page(f"{j.title} at {j.company.name} ({j.location}) · Sinag Jobs", desc, canonical, body,
                 noindex=not hosted, ld=ld)


def _list_page(db: Session, request: Request, category: str | None, location: str | None, page: int) -> HTMLResponse:
    q = select(Job).options(joinedload(Job.company)).join(Company).where(public_job_filter())
    if category:
        q = q.where(Job.category == category)
    if location:
        q = q.where(Job.location == location)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    jobs = db.scalars(q.order_by(Job.posted_at.desc()).offset((page - 1) * PAGE).limit(PAGE)).all()
    what = f"{category} jobs" if category else "Jobs"
    where = f" in {location}" if location and location != "Remote (PH-based)" else (" you can do remotely from the Philippines" if location else " in the Philippines")
    h1 = what + where
    path = "/jobs" + (f"/c/{slug(category)}" if category else "") + (f"/l/{slug(location)}" if location else "")
    canonical = _abs(path + (f"?page={page}" if page > 1 else ""))
    crumbs = [("Jobs", "/jobs")] + ([(category, f"/jobs/c/{slug(category)}")] if category else []) + \
             ([(location, path)] if location else [])
    rows = "".join(f"""<a class="rowcard" href="{job_path(j)}" style="text-decoration:none;color:inherit"><div style="min-width:0">
      <h3>{E(j.title)}</h3><div class="meta"><span>{E(j.company.name)}</span><span>{E(j.city or j.location)}</span><span class="pill">{E(j.setup)}</span>{'<span class="pill easy">Easy Apply</span>' if j.origin == "direct" and j.easy_apply else ''}</div></div>
      <div class="side"><span class="salary">{E(_pay(j).replace(" a month", "")) if j.salary_max else '<small>Salary not stated</small>'}</span></div></a>""" for j in jobs)
    pager = ""
    if total > PAGE:
        pages = (total + PAGE - 1) // PAGE
        pager = '<div class="actions">' + (f'<a class="btn btn-ghost" href="{path}?page={page - 1}" rel="prev">Newer</a>' if page > 1 else "") + \
                (f'<a class="btn btn-ghost" href="{path}?page={page + 1}" rel="next">Older</a>' if page < pages else "") + "</div>"
    intro = (f"{total} open {what.lower()}{where}, updated every 15 minutes from company career pages and verified employers. "
             "Pay is shown in pesos per month.")
    related = ""
    if category and not location:
        locs = db.execute(select(Job.location, func.count()).join(Company).where(public_job_filter(), Job.category == category)
                          .group_by(Job.location).order_by(func.count().desc()).limit(12)).all()
        related = "<h2 style='font-size:20px;margin-top:28px'>By location</h2><p>" + " · ".join(
            f'<a href="/jobs/c/{slug(category)}/l/{slug(l)}">{E(l)} ({n})</a>' for l, n in locs) + "</p>"
    if location and not category:
        cats = db.execute(select(Job.category, func.count()).join(Company).where(public_job_filter(), Job.location == location)
                          .group_by(Job.category).order_by(func.count().desc()).limit(14)).all()
        related = "<h2 style='font-size:20px;margin-top:28px'>By field</h2><p>" + " · ".join(
            f'<a href="/jobs/c/{slug(c)}/l/{slug(location)}">{E(c)} ({n})</a>' for c, n in cats) + "</p>"
    body = f"""{_crumb_html(crumbs)}<h1 style="font-size:clamp(28px,4vw,40px);margin-bottom:8px">{E(h1)}</h1>
<p class="hint" style="max-width:70ch;margin:0 0 20px">{E(intro)}</p>
<div class="rows">{rows or '<div class="empty-sm">No open jobs here right now. <a href="/#alerts">Get an alert</a> when one is posted.</div>'}</div>{pager}{related}"""
    item_list = {"@context": "https://schema.org", "@type": "ItemList", "itemListElement": [
        {"@type": "ListItem", "position": i + 1, "url": _abs(job_path(j))} for i, j in enumerate(jobs)]}
    return _page(f"{h1} ({total} open) · Sinag Jobs", intro, canonical, body, noindex=total == 0,
                 ld=[_crumbs(crumbs), item_list])


@router.get("/jobs")
def directory(db: Session = Depends(get_session)):
    cats = db.execute(select(Job.category, func.count()).join(Company).where(public_job_filter())
                      .group_by(Job.category).order_by(func.count().desc())).all()
    locs = db.execute(select(Job.location, func.count()).join(Company).where(public_job_filter())
                      .group_by(Job.location).order_by(func.count().desc())).all()
    total = sum(n for _, n in cats)
    body = f"""<h1 style="font-size:clamp(28px,4vw,40px);margin-bottom:8px">Jobs in the Philippines</h1>
<p class="hint" style="max-width:70ch">{total} open jobs from verified employers and company career pages. Browse by field or by location.</p>
<div class="co-grid"><section><h2 class="section-title">By field</h2><div class="rows">{"".join(f'<a class="rowcard" style="text-decoration:none;color:inherit" href="/jobs/c/{slug(c)}"><h3>{E(c)}</h3><div class="side"><b>{n}</b></div></a>' for c, n in cats)}</div></section>
<section><h2 class="section-title">By location</h2><div class="rows">{"".join(f'<a class="rowcard" style="text-decoration:none;color:inherit" href="/jobs/l/{slug(l)}"><h3>{E(l)}</h3><div class="side"><b>{n}</b></div></a>' for l, n in locs)}</div></section></div>"""
    return _page("Jobs in the Philippines by field and location · Sinag Jobs",
                 f"{total} open jobs in the Philippines. Browse by field or location.", _abs("/jobs"), body,
                 ld=[_crumbs([("Jobs", "/jobs")])])


def _not_found() -> HTMLResponse:
    return _page("Page not found · Sinag Jobs", "Page not found", _abs("/jobs"),
                 '<h1>Page not found</h1><p><a href="/jobs">See all job categories</a></p>', noindex=True, status=404)


@router.get("/jobs/c/{cat}")
def category_page(cat: str, request: Request, page: int = Query(1, ge=1, le=200), db: Session = Depends(get_session)):
    return _list_page(db, request, CAT_BY_SLUG[cat], None, page) if cat in CAT_BY_SLUG else _not_found()


@router.get("/jobs/l/{loc}")
def location_page(loc: str, request: Request, page: int = Query(1, ge=1, le=200), db: Session = Depends(get_session)):
    return _list_page(db, request, None, LOC_BY_SLUG[loc], page) if loc in LOC_BY_SLUG else _not_found()


@router.get("/jobs/c/{cat}/l/{loc}")
def category_location_page(cat: str, loc: str, request: Request, page: int = Query(1, ge=1, le=200),
                           db: Session = Depends(get_session)):
    if cat not in CAT_BY_SLUG or loc not in LOC_BY_SLUG:
        return _not_found()
    return _list_page(db, request, CAT_BY_SLUG[cat], LOC_BY_SLUG[loc], page)


@router.get("/sitemap.xml")
def sitemap(db: Session = Depends(get_session)):
    urls = [(_abs("/"), None), (_abs("/jobs"), None)]
    pairs = db.execute(select(Job.category, Job.location).join(Company).where(public_job_filter()).distinct()).all()
    for c in sorted({c for c, _ in pairs}):
        urls.append((_abs(f"/jobs/c/{slug(c)}"), None))
    for l in sorted({l for _, l in pairs}):
        urls.append((_abs(f"/jobs/l/{slug(l)}"), None))
    for c, l in sorted(pairs):
        urls.append((_abs(f"/jobs/c/{slug(c)}/l/{slug(l)}"), None))
    for j in db.scalars(select(Job).join(Company).where(public_job_filter(), Job.origin == "direct")
                        .order_by(Job.posted_at.desc()).limit(40000)):
        urls.append((_abs(job_path(j)), aware(j.posted_at).date().isoformat()))
    for c in db.scalars(select(Company).where(Company.source == "direct", Company.approval == "approved", Company.active.is_(True))):
        urls.append((_abs(f"/company.html?c={c.slug}"), None))
    body = "".join(f"<url><loc>{E(u)}</loc>{f'<lastmod>{d}</lastmod>' if d else ''}</url>" for u, d in urls)
    return Response('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    + body + "</urlset>", media_type="application/xml")


@router.get("/robots.txt")
def robots():
    return PlainTextResponse(
        "User-agent: *\nAllow: /\n"
        "Disallow: /api/\nDisallow: /seeker.html\nDisallow: /employer.html\nDisallow: /admin.html\n"
        "Disallow: /checkout.html\nDisallow: /test.html\nDisallow: /chat.html\nDisallow: /join.html\nDisallow: /reset.html\n"
        f"\nSitemap: {settings.public_url}/sitemap.xml\n")
