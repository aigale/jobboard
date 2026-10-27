import asyncio

import pytest
from fastapi.testclient import TestClient

from app import api as api_module
from app.crawler import crawl
from app.db import get_session
from app.demo import ensure_demo_companies, fixture_transport


@pytest.fixture
def client(factory, cfg):
    with factory() as s:
        ensure_demo_companies(s)
    asyncio.run(crawl(factory, cfg, transport=fixture_transport()))

    def _session():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    from app import mailer
    mailer.default_factory = factory
    api_module.app.dependency_overrides[get_session] = _session
    with TestClient(api_module.app) as c:
        yield c
    api_module.app.dependency_overrides.clear()
    mailer.default_factory = None


def test_list_and_filters(client):
    data = client.get("/api/jobs").json()
    assert data["total"] == 12
    times = [j["posted_at"] for j in data["jobs"]]
    assert times == sorted(times, reverse=True)

    assert client.get("/api/jobs", params={"setup": "Remote"}).json()["total"] == 5
    assert client.get("/api/jobs", params={"location": "Iloilo"}).json()["total"] == 2
    assert client.get("/api/jobs", params={"shift": "Night shift"}).json()["total"] == 3
    assert client.get("/api/jobs", params={"perk": ["13th month", "Rice allowance"]}).json()["total"] == 3
    assert client.get("/api/jobs", params={"q": "nurse"}).json()["total"] == 1
    assert client.get("/api/jobs", params={"q": "bayani"}).json()["total"] == 2

    paid = client.get("/api/jobs", params={"min_pay": 60000, "sort": "pay"}).json()["jobs"]
    assert [j["title"] for j in paid] == ["Senior React Developer", "Product Designer (UX/UI)", "Executive Virtual Assistant (US Real Estate)"]


def test_paging_and_saved_ids(client):
    p1 = client.get("/api/jobs", params={"page_size": 5}).json()
    p3 = client.get("/api/jobs", params={"page_size": 5, "page": 3}).json()
    assert len(p1["jobs"]) == 5 and len(p3["jobs"]) == 2
    ids = f"{p1['jobs'][0]['id']},{p1['jobs'][1]['id']}"
    assert client.get("/api/jobs", params={"ids": ids}).json()["total"] == 2


def test_job_detail_and_404(client):
    first = client.get("/api/jobs").json()["jobs"][0]
    d = client.get(f"/api/jobs/{first['id']}").json()
    assert d["description_html"] and d["apply_url"].startswith("https://")
    r = client.get("/api/jobs/999999")
    assert r.status_code == 404 and "no longer open" in r.json()["detail"]


def test_facets(client):
    f = client.get("/api/facets").json()
    assert f["total_open"] == 12 and f["companies"] == 6 and f["last_crawl_at"]
    assert {x["value"]: x["count"] for x in f["setup"]} == {"Remote": 5, "Hybrid": 4, "On-site": 3}
    assert f["perk"][0] == {"value": "13th month", "count": 7}


def test_alerts(client):
    r = client.post("/api/alerts", json={"email": "Juan@Example.com", "category": "Engineering"})
    assert r.status_code == 201 and "confirmation link" in r.json()["message"] and "juan@example.com" in r.json()["message"]
    bad = client.post("/api/alerts", json={"email": "not-an-email"})
    assert bad.status_code == 422


def test_admin_requires_token(client):
    assert client.get("/api/admin/status").status_code == 401


def test_site_is_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "Sinag" in r.text and 'src="app.js"' in r.text
    assert client.get("/app.js").status_code == 200
