import dataclasses

import pytest

from app.config import settings
from app.normalize import normalize, parse_salary_text, salary_to_php_month
from app.sources.base import RawJob, StructuredSalary


def job(**kw):
    base = dict(external_id="1", title="Customer Support Representative", url="https://x/1")
    base.update(kw)
    return RawJob(**base)


@pytest.mark.parametrize("location,bucket,city", [
    ("Makati City, Metro Manila, Philippines", "Metro Manila", "Makati"),
    ("BGC, Taguig", "Metro Manila", "Taguig"),
    ("Cebu IT Park, Cebu City", "Cebu", "Cebu City"),
    ("Iloilo City, Philippines", "Iloilo", "Iloilo City"),
    ("Clark Freeport Zone, Pampanga", "Clark, Pampanga", "Clark"),
    ("Cagayan de Oro", "Cagayan de Oro", "Cagayan de Oro"),
    ("Philippines", "Philippines (other)", None),
])
def test_philippine_locations(location, bucket, city):
    n = normalize(job(location=location), settings)
    assert n and n.location == bucket and n.city == city and n.ph_eligibility == "ph_location"


@pytest.mark.parametrize("location", ["San Francisco, CA", "London", "Remote - US", "Bangalore, India"])
def test_non_philippine_jobs_are_skipped(location):
    assert normalize(job(location=location), settings) is None


def test_country_code_counts():
    assert normalize(job(location="Remote", country_codes=["PH"]), settings).ph_eligibility == "ph_remote"


def test_ph_mention_in_remote_description():
    n = normalize(job(location="Remote", description_text="Open to candidates in the Philippines only."), settings)
    assert n.ph_eligibility == "ph_remote" and n.location == "Remote (PH-based)"


def test_apac_and_worldwide_switches():
    apac = job(location="Remote (APAC)")
    world = job(location="Remote - Worldwide")
    assert normalize(apac, settings) is not None
    assert normalize(world, settings) is None
    assert normalize(apac, dataclasses.replace(settings, include_apac_remote=False)) is None
    assert normalize(world, dataclasses.replace(settings, include_worldwide_remote=True)) is not None


def test_uppercase_ph_token_only():
    assert normalize(job(location="Remote, PH"), settings) is not None
    assert normalize(job(location="Philadelphia, PA"), settings) is None


@pytest.mark.parametrize("text,shift", [
    ("Night shift, US hours", "Night shift"),
    ("You will work EST hours", "Night shift"),
    ("We operate 24/7 on a rotating shift", "Rotating"),
    ("Day shift, 8am to 5pm", "Day shift"),
    ("Support customers in Asia-Pacific time zones", "Not stated"),
    ("Australian business hours", "Not stated"),
])
def test_shift(text, shift):
    assert normalize(job(location="Manila", description_text=text), settings).shift == shift


@pytest.mark.parametrize("title,text,level", [
    ("Senior Backend Engineer", "", "Senior"),
    ("Staff Nurse", "", "Mid level"),
    ("Staff Software Engineer", "", "Senior"),
    ("Account Manager", "", "Mid level"),
    ("Engineering Manager", "", "Senior"),
    ("Junior Accountant", "", "Entry level"),
    ("Content Writer", "Fresh graduates are welcome", "Entry level"),
    ("Analyst", "At least 6 years of relevant experience", "Senior"),
])
def test_level(title, text, level):
    assert normalize(job(title=title, location="Manila", description_text=text), settings).level == level


@pytest.mark.parametrize("title,cat", [
    ("Executive Virtual Assistant", "Virtual Assistant"), ("Staff Nurse", "Healthcare"),
    ("Technical Support Representative", "Customer Support"), ("Data Engineer", "Data"),
    ("Graphic Designer, Social Media", "Design"), ("SEO Specialist", "Marketing"),
    ("Payroll Officer", "Finance"), ("React Developer", "Engineering"), ("Talent Acquisition Lead", "HR & Recruiting"),
])
def test_category(title, cat):
    assert normalize(job(title=title, location="Manila"), settings).category == cat


def test_perks():
    text = "HMO from day 1 for you and 2 dependents. 13th month pay, night differential, rice allowance, PTO."
    perks = normalize(job(location="Manila", description_text=text), settings).perks
    assert perks[:5] == ["HMO day 1", "13th month", "Night diff", "Paid leave", "Rice allowance"]


@pytest.mark.parametrize("text,cur,lo,hi,interval", [
    ("Salary: PHP 30,000 - 40,000 per month", "PHP", 30000, 40000, "MONTH"),
    ("Offer: ₱35k–45k monthly", "PHP", 35000, 45000, "MONTH"),
    ("Basic pay P25,000", "PHP", 25000, 25000, "MONTH"),
    ("Pay rate: $5 - $7 per hour", "USD", 5, 7, "HOUR"),
    ("Compensation USD 1,200 to 1,500 / month", "USD", 1200, 1500, "MONTH"),
    ("Salary range PHP 900,000 - 1,200,000 annually", "PHP", 900000, 1200000, "YEAR"),
])
def test_salary_text(text, cur, lo, hi, interval):
    s = parse_salary_text(text)
    assert (s.currency, s.min, s.max, s.interval) == (cur, lo, hi, interval)


def test_salary_ignores_funding_and_unrelated_dollars():
    assert parse_salary_text("We raised $20M from investors.") is None
    assert parse_salary_text("Our customers saved $300 last year.") is None


def test_salary_conversion():
    lo, hi, orig = salary_to_php_month(StructuredSalary("USD", 5, 7, "HOUR"), settings)
    assert (lo, hi) == (51000, 71500) and orig == "USD 5.00–7.00 / hour"
    lo, hi, _ = salary_to_php_month(StructuredSalary("PHP", 1_200_000, 1_560_000, "YEAR"), settings)
    assert (lo, hi) == (100000, 130000)
    assert salary_to_php_month(StructuredSalary("JPY", 1, 2, "MONTH"), settings)[:2] == (None, None)


def test_description_is_sanitized():
    n = normalize(job(location="Manila", description_html='<p onclick="x()">Hi</p><script>alert(1)</script><a href="https://a">l</a>'), settings)
    assert "script" not in n.description_html and "onclick" not in n.description_html
    assert 'rel="noopener nofollow"' in n.description_html
