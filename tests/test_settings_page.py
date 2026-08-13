"""The Settings page: résumé header details, theme, the configuration readout,
and the one destructive action (delete all my data)."""

from app.db import get_conn
from app.services import jobs as J
from app.services import profile as P
from app.services import users as U


def _seed_data():
    P.create_manual_fact(user_id=1, kind="skill", name="Python")
    job_id = J.create_job("Backend role at Acme Corp", user_id=1)
    J.run_intake(job_id)
    return job_id


def test_page_renders_the_live_provider_configuration(client):
    resp = client.get("/app/account")
    assert resp.status_code == 200
    # conftest pins the mock provider, so that's what the page must report.
    assert "mock" in resp.text


def test_saving_the_resume_header(client):
    resp = client.post(
        "/app/account/name",
        data={"first_name": "  Calvin  ", "last_name": " Schultz ",
              "contact_email": "me@example.com", "contact_phone": "555-0100"},
        headers={"hx-request": "true"},
    )
    assert resp.status_code == 200
    user = U.get_user(1)
    assert user["name"] == "Calvin Schultz"          # trimmed and derived
    assert user["contact_email"] == "me@example.com"
    assert user["contact_phone"] == "555-0100"


def test_blank_first_name_is_refused(client):
    resp = client.post(
        "/app/account/name", data={"first_name": "   "}, headers={"hx-request": "true"}
    )
    assert "first name" in resp.headers.get("HX-Trigger", "")
    assert U.get_user(1)["name"] == ""


def test_theme_is_stored_and_stamped_on_the_page(client):
    client.post("/app/account/display", data={"theme": "dark"},
                headers={"hx-request": "true"})
    assert U.get_user(1)["theme"] == "dark"
    assert 'data-theme="dark"' in client.get("/app").text


def test_invalid_theme_is_refused(client):
    client.post("/app/account/display", data={"theme": "neon"},
                headers={"hx-request": "true"})
    assert U.get_user(1)["theme"] == "system"


# ── Delete all my data ───────────────────────────────────────────────────────


def test_reset_requires_the_exact_confirmation(client, scalar):
    _seed_data()
    resp = client.post("/app/account/reset", data={"confirm": "delete please"})
    assert resp.status_code == 422
    assert scalar("SELECT COUNT(*) FROM jobs") == 1  # nothing removed


def test_reset_clears_every_user_scoped_table(client, scalar):
    _seed_data()
    resp = client.post("/app/account/reset", data={"confirm": "DELETE"},
                       follow_redirects=False)
    assert resp.status_code == 303
    for table in ("jobs", "profile_facts", "documents", "questions",
                  "interview_sessions", "insights", "llm_requests"):
        assert scalar(f"SELECT COUNT(*) FROM {table}") == 0, table
    # The profile row itself survives, so the app still works afterwards.
    assert U.get_user(1) is not None
    assert client.get("/app").status_code == 200


def test_reset_deletes_the_uploaded_files_too(client, tmp_path):
    from app.services.storage import get_storage

    storage = get_storage()
    storage.save("abc.txt", b"resume bytes")
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO documents (user_id, filename, path, status) "
            "VALUES (1, 'cv.txt', 'abc.txt', 'ready')"
        )
        conn.commit()
    finally:
        conn.close()
    U.reset_data(1)
    try:
        storage.read("abc.txt")
        raise AssertionError("the uploaded file should be gone")
    except FileNotFoundError:
        pass


def test_reset_list_covers_every_table_with_a_user_id(scalar):
    """A new user-scoped table that users._USER_TABLES misses would silently
    survive 'delete all my data'. Catch that here rather than in a bug report."""
    conn = get_conn()
    try:
        tables = [
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        scoped = {
            t for t in tables
            if any(c["name"] == "user_id"
                   for c in conn.execute(f"PRAGMA table_info({t})").fetchall())
        }
    finally:
        conn.close()
    missed = scoped - set(U._USER_TABLES)
    assert missed == set(), f"tables with user_id not cleared by reset_data: {missed}"
