"""Start a disposable Sinag Jobs server for Playwright.

Playwright's webServer starts this file. It creates a fresh SQLite database in
e2e/.tmp, seeds the demo accounts plus the offline sample jobs (the public demo
seed does not create listings), then serves the app on 127.0.0.1:8010. Nothing
here touches the developer's sinag.db or uploads/.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TMP = HERE / ".tmp"
DB_PATH = TMP / "sinag-e2e.db"
UPLOAD_PATH = TMP / "uploads"
PORT = int(os.getenv("E2E_PORT", "8010"))
BASE_URL = f"http://127.0.0.1:{PORT}"


def test_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": f"sqlite:///{DB_PATH.as_posix()}",
            "UPLOAD_DIR": str(UPLOAD_PATH),
            "PUBLIC_URL": BASE_URL,
            "CORS_ORIGINS": BASE_URL,
            "DOMAIN": "localhost",
            "COOKIE_SECURE": "false",
            "REQUIRE_EMAIL_VERIFICATION": "true",
            "BILLING_ENABLED": "true",
            "PAYMENT_MODE": "sandbox",
            "SMTP_HOST": "",
            "SMTP_USER": "",
            "SMTP_PASSWORD": "",
            "SMTP_FROM": "Sinag Jobs <no-reply@localhost>",
            "ANTHROPIC_API_KEY": "",
            "CRAWL_INTERVAL_MINUTES": "9999",
            "PYTHONUNBUFFERED": "1",
        }
    )
    return env


def seed_demo() -> None:
    if TMP.exists():
        shutil.rmtree(TMP)
    UPLOAD_PATH.mkdir(parents=True, exist_ok=True)
    print(f"Seeding disposable E2E database at {DB_PATH}", flush=True)
    subprocess.run(
        [sys.executable, "-m", "app.cli", "demo", "--sample-jobs"],
        cwd=ROOT,
        env=test_env(),
        check=True,
    )
    print("E2E database seeded.", flush=True)


def port_is_free() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", PORT))
        except OSError:
            return False
    return True


if __name__ == "__main__":
    if not port_is_free():
        raise SystemExit(f"Port {PORT} is already in use; stop that server or set E2E_PORT.")
    seed_demo()

    # The seed subprocess got the test environment explicitly; the server process
    # must get it too, or app.config would load .env and point DATABASE_URL at a
    # relative SQLite file under e2e/. The process runs with e2e/ as its working
    # directory, so also make the project root importable before uvicorn imports
    # app.api.
    os.environ.update(test_env())
    sys.path.insert(0, str(ROOT))
    import uvicorn

    # Run uvicorn in this process so Playwright can stop the server cleanly.
    uvicorn.run("app.api:app", host="127.0.0.1", port=PORT, log_level="warning")
