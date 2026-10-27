# Sinag Jobs

A job board for the Philippines. It collects openings from company career pages, and lets
employers post jobs that candidates apply to right on the site.

**What's included**

| For | Features |
|---|---|
| Job seekers | Accounts with email confirmation; search by keyword, location, field, industry, job type, setup, shift, level, pay range and benefits; a reusable profile with resumes, certificates and portfolios; "Jobs for you" match scores; daily or instant email alerts; browser push notifications; Easy Apply; status tracking with a timeline; skills tests and follow-up chats from employers; invitations from employers; optional listing in the resume database; account deletion |
| Employers | Company verified by an admin; branded public company page (logo, cover, tagline, values, team quotes, video tour, photos, links); team members with email invites; post, edit, schedule and close jobs; screening questions; timed skills tests, graded automatically; a follow-up screening chat (scripted, or AI-led with a Claude API key); an applicant board with match scores, test results, tags, notes, messages and an optional AI fit summary; resume database search with invites |
| Admins | Approve or reject employers; crawler status; site totals |

Phase 3 adds billing with PayMongo, analytics, and SEO and XML/CSV imports.

## Try it in two minutes (no internet needed)

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m app.cli demo            # demo accounts; no fake listings
uvicorn app.api:app --reload      # open http://localhost:8000
```

The demo command creates three accounts and an approved demo company, but it does
not add fake or sample jobs, so the board shows only real crawl and feed listings.
The password for all three is `sinag-demo-123`:

| Sign in as | To try |
|---|---|
| `seeker@demo.sinag` | Upload a resume, browse real listings, and use Easy Apply on an employer-posted job |
| `employer@demo.sinag` | Post a job, review applicants, search the resume database, edit the company page, invite a teammate |
| `admin@demo.sinag` | Approve a new employer at `/admin.html` |

For offline UI work, `python -m app.cli demo --sample-jobs` also loads 12 recorded
jobs from six fictional companies and three Easy Apply jobs. That option is for
local testing only. If an older database still has those sample listings, remove
them with `python -m app.cli remove-demo-jobs`; real feed jobs are left alone.

Emails aren't sent on your computer; read them with `python -m app.cli outbox`.

## Crawl real companies

```bash
cp .env.example .env
# add real companies to companies.yaml (see companies.example.yaml for the format)
python -m app.cli crawl                     # one crawl now, prints what changed
python -m app.cli status                    # job counts per company and any errors
python -m app.worker                        # keep crawling every CRAWL_INTERVAL_MINUTES
```

Each company needs a `name`, a `source` and a `key`. The comments in
`companies.example.yaml` show where to find the key on each careers site. A good way to
build the list: search for companies hiring in the Philippines, open their careers page,
and note which system the job links point to (boards.greenhouse.io, jobs.lever.co, and so on).

`companies.yaml` also ships with seven free, no-key aggregator feeds: Himalayas, Arbeitnow,
Remote OK, Jobicy, Remotive, Working Nomads and We Work Remotely. They are a quick way to
populate a new board. Unlike an ATS source, a feed mixes employers, so each job is attributed
to the employer named in the feed and the normal Philippine/APAC/Worldwide filter still
applies. For a production board, add direct employer ATS sources as well. Adzuna and
TheirStack need an account/API key; the Bundesagentur für Arbeit feed only covers jobs in
Germany, so it is not useful for this board.

## Run in production

```bash
cp .env.example .env              # set DOMAIN, POSTGRES_PASSWORD, ADMIN_TOKEN and the SMTP_ settings
docker compose up -d --build      # Caddy (HTTPS) + website/API + crawler worker + Postgres
docker compose exec api python -m app.cli create-admin you@yourdomain.ph
```

Caddy gets a free HTTPS certificate for `DOMAIN` automatically once the domain points at
the server. See the install and deploy guide for the full server setup.

Upgrading an existing site: pull the new code and run `docker compose up -d --build`. New
database columns and tables are added automatically on start; existing jobs are kept.

**Email.** Password resets, applicant notifications, status updates and job alerts need an
email service. Set `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` and `SMTP_FROM`
in `.env` (Amazon SES, Postmark, Brevo and Google Workspace all work), and set up SPF and
DKIM for your domain with that provider so emails don't land in spam.

**Push notifications** (optional): run `docker compose exec api python -m app.cli vapid-keys`, add the
three lines it prints to `.env`, and run `docker compose up -d`. People then turn push on under
Notifications in their dashboard. On iPhone, push works after adding the site to the Home Screen.

**AI** (optional): add `ANTHROPIC_API_KEY` to `.env` to turn on the AI-led screening chat and the AI
fit summary. Without it, the chat asks the employer's topics in order and everything else works the same.
The AI never scores or rejects anyone; it asks questions and writes summaries for a person to read.

**Backups.** Uploaded files live in the `uploads` Docker volume, separate from the
database. Back up both:
`docker run --rm -v sinag-backend_uploads:/u -v ~/backups:/b alpine tar czf /b/uploads-$(date +%F).tgz -C /u .`

## How the crawler decides

| Question | Rule |
|---|---|
| Is it a Philippine job? | Location, country code, or a PH city names it; or the job is remote and the description says it's open to people in the Philippines. Remote APAC jobs are included and worldwide-remote jobs are left out by default (`INCLUDE_APAC_REMOTE`, `INCLUDE_WORLDWIDE_REMOTE`). |
| Location shown | Specific city first (Makati, Taguig, Cebu City, Iloilo City…), grouped into a region for the filter (Metro Manila, Cebu, Davao, Iloilo, Clark…). Remote jobs show as "Remote (PH-based)". |
| Salary | Structured salary from the source when present, otherwise found in the description (`PHP 30,000 - 40,000`, `₱35k–45k monthly`, `$5-$7 per hour`). Converted to pesos per month with the rates in `.env`; the original wording is kept and shown. |
| Shift | Night shift for US hours, EST/PST, graveyard or night differential; Rotating for 24/7 or shifting schedules; Day shift when stated; otherwise "Not stated". |
| Level, category, benefits | Keywords in the title and description (HMO, 13th month, night diff, rice allowance, paid leave…). |
| When a job disappears | Closed only after it is missing from 2 successful crawls in a row (`CLOSE_AFTER_MISSED_CRAWLS`). If a source fails, that company's jobs stay as they are. A job that comes back is reopened with the same id, so shared links keep working. |

All of these rules live in `app/normalize.py` as plain keyword lists, and `tests/test_normalize.py` has an example for each one. To change a rule, add a test case first.

## Being a good crawler

- Uses the official public job board APIs where they exist instead of scraping HTML.
- Checks `robots.txt` before reading any other careers page.
- Waits `PER_HOST_DELAY_SECONDS` between requests to the same site and backs off on HTTP 429 and 5xx errors.
- Sends an honest `USER_AGENT` with a contact address. Change it to your real domain and email before going live.
- Links every job to the company's own page, so applicants apply at the source.

## API

Interactive docs are at `/docs` when the server is running.

| Endpoint | What it returns |
|---|---|
| `GET /api/jobs` | Open jobs. Filters: `q`, `location`, `category`, `industry`, `job_type`, `setup`, `shift`, `level`, `perk` (each can repeat), `min_pay`, `max_pay` (₱/month), `easy_apply`, `company`, `ids`; `sort=new\|pay`; `page`, `page_size`. |
| `GET /api/jobs/{id}` | One job with its description, screening questions, and (when signed in) your match and application status. |
| `POST /api/jobs/{id}/apply` | Easy Apply (job seekers). |
| `/api/auth/*` | Sign up, sign in, sign out, forgot and reset password. |
| `/api/me/*` | Profile, documents, applications, matches, alerts, notifications, account deletion. |
| `/api/employer/*` | Company, jobs, applicant board, notes, files. |
| `/api/admin/overview`, `/api/admin/companies` | Admin dashboard and employer approvals (admin accounts). |
| `GET /api/facets` | Counts for every filter, total open jobs, companies hiring, last crawl time. |
| `GET /api/companies` | Companies with open job counts. |
| `POST /api/alerts` | Saves a job alert subscription: `{"email", "category", "location"}`. |
| `GET /api/admin/status` | Recent crawls and failing companies. Admin account, or `Authorization: Bearer <ADMIN_TOKEN>`. |
| `POST /api/admin/crawl` | Starts a crawl now (optionally `?company=<slug>`). Admin account or the token. |

## Add another job system

Write one async function in `app/sources/<name>.py` that takes a `CompanySpec` and the
shared `Http` client and returns a list of `RawJob`, then register it in
`app/sources/__init__.py`. Add a recorded response to `fixtures/` and a route for it in
`app/demo.py` so it's covered by the tests.

An aggregator source (one feed containing many employers) should also be added to
`FEED_SOURCES` and set `RawJob.company_name` (plus `company_logo` when available). The
crawler then creates one company page per employer, prefixes each external id with the feed
slug so feeds cannot close each other's jobs, and applies the normal location filter.
Set `RawJob.worldwide = True` only when the feed explicitly says the role is open anywhere.

## Billing, analytics, SEO and imports

**Payments are test-only.** Every payment screen says: *"This is a test environment. Do not enter real payment details."*

- `PAYMENT_MODE=sandbox` (default) uses a built-in simulator. Use the test cards shown on the checkout page:
  `4242 4242 4242 4242` (succeeds), `5555 5555 5555 4444` (succeeds), `4000 0000 0000 0002` (declined),
  `4000 0000 0000 9995` (insufficient funds), any future expiry, any 3-digit CVC. GCash and Maya are simulated with
  "success" and "cancel" buttons. Real-looking card numbers are refused and never stored.
- `PAYMENT_MODE=paymongo_test` uses PayMongo's hosted checkout in **test mode only**: keys must start with `sk_test_`,
  and webhooks marked live are ignored. Point the PayMongo test webhook at `/api/billing/webhooks/paymongo`.
- Products (admin can change prices): single post ₱1,499, 5 posts ₱5,999, featured 7 days ₱499,
  Starter ₱2,999/30 days, Pro ₱7,999/30 days. Each company gets `FREE_LIVE_JOBS` live jobs free.
- Order numbers look like `SJ-TEST-000001` and receipts say "TEST RECEIPT: no real charges were made."

**Analytics** count visits, unique visitors, job views, apply clicks, applications, searches and sign-ups per day,
with no cookies. Unique visitors use a daily salted hash that is deleted after 2 days. Employers see their own
jobs under *Analytics*; admins see the whole site plus recruiter engagement.

**SEO**: each job has its own page at `/jobs/{id}/{title}` with Google for Jobs `JobPosting` data (employer posts only;
crawled jobs point Google to the original). Category and city pages live under `/jobs/c/...` and `/jobs/l/...`,
plus `/sitemap.xml` and `/robots.txt`.

**Imports**: admins can upload a CSV ([template](/api/import/template.csv)) or Indeed-style XML file for any company,
check it first, then import. Feeds (a URL) re-sync daily and close jobs that disappear. Pro-plan employers can
bulk import their own jobs from the *Job posts* tab.

## Messages, interviews, sign-in options, extras, languages and the phone app

- **Messages:** one conversation per application. Employers write first; the applicant can reply after
  that. Each new message sends an in-app notification and push; email goes out once per unread batch.
- **Interviews:** from an applicant's panel, offer up to 3 times (Philippine time) for a video call, phone
  call or in-person meeting. The applicant picks one or asks for other times. Confirmed interviews email
  both sides an `.ics` calendar invite (Google Calendar, Outlook, Apple Calendar) and show an
  "Add to Google Calendar" link. Changes and cancellations update the same calendar event. Both sides get
  a reminder about 24 hours before.
- **Sign in with Google, Facebook or LinkedIn:** set the keys in `.env` (see `.env.example`) and the
  buttons appear. Accounts are matched by verified email. Admins keep password sign-in.
- **Job seeker extras (test payments):** a highlighted profile for 30 days (₱299: shows first in employer
  candidate searches, with a "Highlighted" badge) and 5 priority applications (₱149: shown first on the
  employer's board, with a "Priority" badge). Both are labelled to employers as paid options.
- **Discount codes:** admins create codes under Admin, then Billing: percent or peso off, for everyone,
  employers or job seekers, one product or all, with an end date, a use limit and once-per-account.
  A code that brings the price to zero completes the order with no payment.
- **Invoices:** every paid order has a printable invoice (`/invoice.html?order=…`, print or save as PDF)
  with seller and buyer details, discount and VAT lines. They are marked **TEST: not a BIR-registered
  invoice or official receipt**. Set `SELLER_NAME`, `SELLER_ADDRESS`, `SELLER_TIN` and `VAT_REGISTERED`.
- **Tagalog and Cebuano:** a language picker in the header switches the site. Translations live in
  `i18n_src/` (English → Tagalog, Cebuano); run `python i18n_src/build.py` after editing them, which
  writes `static/i18n/tl.json` and `static/i18n/ceb.json`. Job posts, names and the privacy notice stay as
  written. Have native speakers review the wording before launch.
- **Phone app:** see `mobile/README.md` (Android and iPhone, built with Capacitor, with native push
  through Firebase).

## Tests

```bash
pytest
```

The tests run every source against recorded responses, check the open/update/close/reopen
lifecycle, confirm a failed source never closes jobs, and cover accounts, password reset,
uploads, Easy Apply, screening questions, the applicant board, who can see what, scheduling,
approvals, alerts, account deletion, upgrading an old database, email confirmation, encrypted push
delivery, team invites, company pages, the resume database and its privacy rules, timed tests, and the
screening chat with and without AI (using a fake AI service, so no API key is needed).

To run them on Postgres as well: `TEST_DATABASE_URL=postgresql+psycopg://user:pass@host/db pytest`
(GitHub runs both on every push).

## Not built yet

- **AI video interviews:** left out on purpose (Data Privacy Act and cost). Revisit after launch.
- **Translated emails.** Emails are still in English; the site itself is translated.
- **Live payments.** Billing runs in test mode only. Going live needs a PayMongo business account review,
  official receipts (BIR), and removing the test-key guard on purpose. Do not do this without that review.
- **Full-text search.** Search uses simple matching, which is fine for tens of thousands of
  jobs. Move to Postgres full-text search when the board grows past that.
