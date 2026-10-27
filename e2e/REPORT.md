# Sinag Jobs verification report

**Date:** 2026-10-28  
**Scope:** public board, search and filters, job modal, accounts and role journeys, seeker apply,
employer pipeline and job posting, admin approval, theme invariants, responsive layout,
console errors, API contract, accessibility, and demo-seed cleanup.

## Results

| Check | Result |
|---|---|
| Python regression suite | 151 passed |
| Playwright Chromium suite | 22 passed |
| Playwright Firefox smoke | 2 passed |
| Playwright WebKit smoke | 2 passed |
| Playwright total | 26 passed, 0 failed, 0 flaky |
| Axe audit | 0 violations on home light/dark, login, seeker, employer, and admin pages |

The browsers run against a disposable SQLite database and upload directory in `e2e/.tmp`.
The developer database on port 8000 and `sinag.db` are not used by the suite.

## Coverage

- `00-smoke`: board load, search, filters, theme persistence in Chromium, Firefox, and WebKit.
- `01-public`: live stats, keyword and setup filters, empty state, reset, job modal, save/unsave,
  Escape close, server-rendered SEO page, sitemap, theme text/image/layout invariants, mobile
  layout, API paging and write protection.
- `02-seeker`: sign-in, resume upload, Easy Apply, submitted application tracking, profile update,
  job alert creation, and new-seeker verification state.
- `03-employer`: applicant pipeline, stage change, draft job, public company page, analytics,
  team tab, and skills-test tab.
- `04-admin`: dashboard tabs, crawler/analytics/billing/import views, employer approval, and
  approved-employer sign-in.
- `05-accessibility`: axe scans for the public board (light/dark), login, seeker, employer, and
  admin dashboards.

## Fixes included in this worktree

- Theme switching is limited to palette/background/border changes; text, images, icons, and
  layout stay unchanged.
- Light-theme contrast corrected for red, green, and gold accents; brand labels standardized.
- Parallel billing summary requests no longer race on first-run product seeding.
- Demo seeding no longer publishes fake listings by default. Sample jobs are opt-in with
  `python -m app.cli demo --sample-jobs` for local tests only.
- `python -m app.cli remove-demo-jobs` removes the old offline sample listings and fictional
  crawl companies without touching real feed jobs.
- The public company directory only lists companies with at least one open job.
- The E2E server seeds sample jobs explicitly for its disposable test database.

## Reproduce

```powershell
.\.venv\Scripts\python.exe -m pytest -q

cd e2e
npm install
npx playwright test
```

Reports are written to `e2e/report/html/index.html`, `e2e/report/results.json`, and
`e2e/report/junit.xml`.

## Caveats

- The feed crawlers are tested with recorded fixtures; the browser suite does not crawl the
  public feeds over the network.
- Email and push delivery are inspected through the outbox/local test doubles, not a live
  provider.
- The accessibility spec is informational and currently reports zero violations; it does not
  fail future runs on new axe findings unless that policy is changed.
