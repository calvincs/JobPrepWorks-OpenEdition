"""Tailored Resume: per-job resume generation (tab, pipeline, versioning,
and the view/download routes)."""

from app.db import get_conn, public_id_of
from app.llm.base import LLMError
from app.services import jobs as J
from app.services import profile as P
from app.services import resume as resume_service


def _ready_job():
    P.create_manual_fact(user_id=1, kind="skill", name="Python")
    j = J.create_job("Backend role", user_id=1)
    J.run_intake(j)
    return j



def test_generate_resume_creates_versioned_row_and_renders(client, scalar):
    j = _ready_job()
    pid = public_id_of("jobs", j)
    r = client.post(f"/app/jobs/{pid}/resume", headers={"hx-request": "true"})
    assert r.status_code == 200
    # TestClient runs the background task synchronously: the resume is done.
    assert scalar("SELECT resume_status FROM jobs WHERE id = ?", j) == "ready"
    assert scalar("SELECT version FROM resumes WHERE job_id = ?", j) == 1
    tab = client.get(f"/app/jobs/{pid}/tab/resume").text
    assert "Payments &amp; Platform" in tab and "Experience" in tab
    assert "View / Print" in tab and "Regenerate" in tab


def test_double_submit_claims_once(client, scalar, monkeypatch):
    # Freeze the background task so the second POST sees 'running' and no-ops.
    monkeypatch.setattr(resume_service, "run_resume", lambda job_id: None)
    from app.routers import jobs as R

    monkeypatch.setattr(R.resume_service, "run_resume", lambda job_id: None)
    j = _ready_job()
    pid = public_id_of("jobs", j)
    client.post(f"/app/jobs/{pid}/resume", headers={"hx-request": "true"})
    client.post(f"/app/jobs/{pid}/resume", headers={"hx-request": "true"})
    assert scalar(
        "SELECT COUNT(*) FROM llm_requests WHERE user_id = 1 AND kind = 'resume'"
    ) == 1  # lost claim isn't charged


def test_regenerate_bumps_version(client, scalar):
    j = _ready_job()
    pid = public_id_of("jobs", j)
    client.post(f"/app/jobs/{pid}/resume", headers={"hx-request": "true"})
    client.post(f"/app/jobs/{pid}/resume", headers={"hx-request": "true"})
    assert scalar("SELECT MAX(version) FROM resumes WHERE job_id = ?", j) == 2
    assert resume_service.latest_resume(j)["version"] == 2


def test_no_profile_prompts_for_facts(client, scalar):
    j = J.create_job("Backend role", user_id=1)
    J.run_intake(j)
    pid = public_id_of("jobs", j)
    tab = client.get(f"/app/jobs/{pid}/tab/resume").text
    assert "Add your background first" in tab
    # Service bails without a profile: status stays 'none', no row written.
    resume_service.run_resume(j)
    assert scalar("SELECT resume_status FROM jobs WHERE id = ?", j) == "none"
    assert scalar("SELECT COUNT(*) FROM resumes WHERE job_id = ?", j) == 0


def test_llm_error_reaches_terminal_error_with_retry(client, scalar, monkeypatch):
    j = _ready_job()

    def boom(**kwargs):
        raise LLMError("The resume writer is unavailable right now.")

    from app.services import resume as RS

    monkeypatch.setattr(RS, "get_provider", lambda: type("X", (), {"extract": staticmethod(boom)})())
    resume_service.run_resume(j)
    assert scalar("SELECT resume_status FROM jobs WHERE id = ?", j) == "error"
    assert scalar("SELECT resume_error FROM jobs WHERE id = ?", j)
    tab = client.get(f"/app/jobs/{public_id_of('jobs', j)}/tab/resume").text
    assert "Retry" in tab


def test_job_delete_cascades_resumes(client, scalar):
    j = _ready_job()
    client.post(f"/app/jobs/{public_id_of('jobs', j)}/resume", headers={"hx-request": "true"})
    assert scalar("SELECT COUNT(*) FROM resumes WHERE job_id = ?", j) == 1
    J.delete_job(j, 1)
    assert scalar("SELECT COUNT(*) FROM resumes WHERE job_id = ?", j) == 0


def test_resume_routes_404_for_other_users_job(client, scalar):
    j = _ready_job()
    pid = public_id_of("jobs", j)
    conn = get_conn()
    try:
        conn.execute("INSERT INTO users (id, name) VALUES (999, 'other') ON CONFLICT DO NOTHING")
        conn.execute("UPDATE jobs SET user_id = 999 WHERE id = ?", (j,))
        conn.commit()
    finally:
        conn.close()
    assert client.post(f"/app/jobs/{pid}/resume").status_code == 404
    assert client.get(f"/app/jobs/{pid}/tab/resume").status_code == 404
    assert client.get(f"/app/jobs/{pid}/resume/view").status_code == 404
    assert client.get(f"/app/jobs/{pid}/resume/download").status_code == 404


# ── Resume-specific routes (no pitch analog) ──






def test_view_route_requires_existing_resume(client, scalar):
    j = _ready_job()
    pid = public_id_of("jobs", j)
    assert client.get(f"/app/jobs/{pid}/resume/view").status_code == 404
    client.post(f"/app/jobs/{pid}/resume", headers={"hx-request": "true"})
    r = client.get(f"/app/jobs/{pid}/resume/view")
    assert r.status_code == 200
    assert "Print / Save as PDF" in r.text
    assert "<html" in r.text.lower()  # standalone doc, not the app shell


def test_download_formats(client, scalar):
    j = _ready_job()
    pid = public_id_of("jobs", j)
    client.post(f"/app/jobs/{pid}/resume", headers={"hx-request": "true"})
    for fmt, expect_ct in (("md", "text/markdown"), ("html", "text/html"), ("txt", "text/plain")):
        r = client.get(f"/app/jobs/{pid}/resume/download?format={fmt}")
        assert r.status_code == 200
        assert expect_ct in r.headers["content-type"]
        assert "attachment" in r.headers["content-disposition"]


def test_download_default_format_is_markdown(client, scalar):
    j = _ready_job()
    pid = public_id_of("jobs", j)
    client.post(f"/app/jobs/{pid}/resume", headers={"hx-request": "true"})
    r = client.get(f"/app/jobs/{pid}/resume/download")
    assert "text/markdown" in r.headers["content-type"]


def test_download_filename_sanitizes_crlf_and_slashes(client, scalar):
    """company/title are LLM-extracted freeform text — unlike download_posting's
    filename (an upload's own name), this one must strip header-injection and
    path-separator characters, not just the quote download_posting strips."""
    j = _ready_job()
    conn = get_conn()
    try:
        conn.execute("UPDATE jobs SET company = ?, title = ? WHERE id = ?",
                     ("Evil\r\nX-Injected: 1", "A/B\\C", j))
        conn.commit()
    finally:
        conn.close()
    pid = public_id_of("jobs", j)
    client.post(f"/app/jobs/{pid}/resume", headers={"hx-request": "true"})
    r = client.get(f"/app/jobs/{pid}/resume/download?format=md")
    cd = r.headers["content-disposition"]
    assert "\r" not in cd and "\n" not in cd and "/" not in cd and "\\" not in cd


def test_regenerating_bumps_the_version_each_time(client, scalar):
    """Nothing caps regeneration — you're spending your own tokens."""
    j = _ready_job()
    pid = public_id_of("jobs", j)
    for _ in range(3):
        r = client.post(f"/app/jobs/{pid}/resume", headers={"hx-request": "true"})
        assert r.status_code == 200
    assert scalar("SELECT MAX(version) FROM resumes WHERE job_id = ?", j) == 3
