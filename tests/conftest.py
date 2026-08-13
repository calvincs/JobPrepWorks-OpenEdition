"""Test harness.

Everything runs against a throwaway SQLite file and the deterministic mock LLM
provider — no database server, no network, no API key. Environment variables
are set here BEFORE any app import because app.config reads them at import
time and freezes them into `settings`.
"""

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="jobprep_tests_")
os.environ["LLM_PROVIDER"] = "mock"
os.environ["JOBPREP_DATA_DIR"] = _TMP
os.environ["JOBPREP_DB"] = os.path.join(_TMP, "test.db")
# No background loops in tests: their sweeps would run in the TestClient's
# lifespan thread and race the per-test wipe. Tests call sweep() directly.
os.environ["PULSE_POLL_INTERVAL"] = "0"
os.environ["LLM_LEDGER_SWEEP_INTERVAL"] = "0"
os.environ["REAPER_INTERVAL"] = "0"
# Pin the spend brake off so a developer's own .env can't fail the suite, and
# pin search to a known state so pulse tests don't depend on which keys are set.
os.environ["LLM_DAILY_LIMIT"] = "0"
os.environ["WEB_SEARCH"] = "native"

import pytest  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from app.config import DB_PATH, DEFAULT_USER_ID  # noqa: E402
from app.db import get_conn, init_db  # noqa: E402
from app.main import app  # noqa: E402

# The fixtures below DELETE every row — refuse to aim that at a real database.
if DB_PATH.parent != __import__("pathlib").Path(_TMP):
    raise RuntimeError(
        f"Refusing to run tests against {DB_PATH}: the test database must live in "
        "the temp directory this conftest created (unset JOBPREP_DB)."
    )


@pytest.fixture(scope="session", autouse=True)
def _schema():
    """Once per run: build the schema in the throwaway database."""
    init_db()
    yield


@pytest.fixture(autouse=True)
def _clean_tables():
    """Isolate every test: wipe data, reset autoincrement, re-seed the user."""
    conn = get_conn()
    try:
        tables = [
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' AND name <> 'schema_migrations'"
            ).fetchall()
        ]
        # Order doesn't matter with foreign keys off for the wipe; turning them
        # back on immediately keeps the cascade behavior tests depend on.
        conn.execute("PRAGMA foreign_keys = OFF")
        for table in tables:
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM sqlite_sequence")
        conn.execute("INSERT INTO users (id, name) VALUES (?, '')", (DEFAULT_USER_ID,))
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")
    finally:
        conn.close()
    yield


@pytest.fixture
def client(_clean_tables):
    """A TestClient with the app's lifespan running. Starlette's TestClient
    executes BackgroundTasks synchronously, so a request that enqueues an LLM
    pipeline has already finished it by the time the response is returned. The
    explicit _clean_tables dependency pins ordering."""
    with TestClient(app) as c:
        yield c


@pytest.fixture
def scalar():
    """Run a query on a fresh connection and return its first column (or None)."""

    def _scalar(sql, *params):
        conn = get_conn()
        try:
            row = conn.execute(sql, params).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    return _scalar
