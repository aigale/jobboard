from sqlalchemy import func, select

from app.demo import DEMO_COMPANIES, DEMO_DIRECT_JOBS, load_demo, load_demo_accounts, remove_demo_jobs
from app.models import Company, Job, User


def _count(s, model):
    return s.scalar(select(func.count()).select_from(model))


def test_demo_accounts_seed_no_fake_listings(factory):
    lines = load_demo_accounts(sample_jobs=False, session_factory=factory)

    with factory() as s:
        assert _count(s, Job) == 0
        assert s.scalar(select(Company).where(Company.slug == "sinag-demo-corp")) is not None
        assert s.scalar(select(User).where(User.email == "employer@demo.sinag")) is not None
        assert s.scalar(select(User).where(User.email == "seeker@demo.sinag")) is not None

    assert any("no sample listings" in line for line in lines)


def test_sample_jobs_are_opt_in_and_removable(factory, cfg):
    load_demo(cfg, session_factory=factory)
    lines = load_demo_accounts(sample_jobs=True, session_factory=factory)

    with factory() as s:
        assert _count(s, Job) == 15
        assert _count(s, Company) == len(DEMO_COMPANIES) + 1
        assert {j.title for j in s.scalars(select(Job).where(Job.origin == "direct"))} == {
            j["title"] for j in DEMO_DIRECT_JOBS
        }

    assert any("3 Easy Apply jobs" in line for line in lines)

    assert remove_demo_jobs(factory) == 15

    with factory() as s:
        assert _count(s, Job) == 0
        assert s.scalar(select(Company).where(Company.slug == "sinag-demo-corp")) is not None
        assert s.scalar(select(Company).where(Company.slug == "kalesa-labs")) is None
        assert s.scalar(select(User).where(User.email == "employer@demo.sinag")) is not None
