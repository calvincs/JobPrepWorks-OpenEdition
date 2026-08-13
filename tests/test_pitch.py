"""Your Pitch: per-job pitch generation (tab, pipeline, versioning, quota)."""

from app.db import get_conn, public_id_of
from app.llm.base import LLMError
from app.services import jobs as J
from app.services import pitch as pitch_service
from app.services import profile as P


def _ready_job():
    P.create_manual_fact(user_id=1, kind="skill", name="Python")
    j = J.create_job("Backend role", user_id=1)
    J.run_intake(j)
    return j



def test_generate_pitch_creates_versioned_row_and_renders(client, scalar):
    j = _ready_job()
    pid = public_id_of("jobs", j)
    r = client.post(f"/app/jobs/{pid}/pitch", headers={"hx-request": "true"})
    assert r.status_code == 200
    # TestClient runs the background task synchronously: the pitch is done.
    assert scalar("SELECT pitch_status FROM jobs WHERE id = ?", j) == "ready"
    assert scalar("SELECT version FROM pitches WHERE job_id = ?", j) == 1
    tab = client.get(f"/app/jobs/{pid}/tab/pitch").text
    assert "15 seconds" in tab and "30 seconds" in tab and "2 minutes" in tab
    assert "Talking points" in tab and "Regenerate" in tab


def test_double_submit_claims_once(client, scalar, monkeypatch):
    # Freeze the background task so the second POST sees 'running' and no-ops.
    monkeypatch.setattr(pitch_service, "run_pitch", lambda job_id: None)
    from app.routers import jobs as R

    monkeypatch.setattr(R.pitch_service, "run_pitch", lambda job_id: None)
    j = _ready_job()
    pid = public_id_of("jobs", j)
    client.post(f"/app/jobs/{pid}/pitch", headers={"hx-request": "true"})
    client.post(f"/app/jobs/{pid}/pitch", headers={"hx-request": "true"})
    assert scalar(
        "SELECT COUNT(*) FROM llm_requests WHERE user_id = 1 AND kind = 'pitch'"
    ) == 1  # lost claim isn't charged


def test_regenerate_bumps_version(client, scalar):
    j = _ready_job()
    pid = public_id_of("jobs", j)
    client.post(f"/app/jobs/{pid}/pitch", headers={"hx-request": "true"})
    client.post(f"/app/jobs/{pid}/pitch", headers={"hx-request": "true"})
    assert scalar("SELECT MAX(version) FROM pitches WHERE job_id = ?", j) == 2
    assert pitch_service.latest_pitch(j)["version"] == 2


def test_no_profile_prompts_for_facts(client, scalar):
    j = J.create_job("Backend role", user_id=1)
    J.run_intake(j)
    pid = public_id_of("jobs", j)
    tab = client.get(f"/app/jobs/{pid}/tab/pitch").text
    assert "Add your background first" in tab
    # Service bails without a profile: status stays 'none', no row written.
    pitch_service.run_pitch(j)
    assert scalar("SELECT pitch_status FROM jobs WHERE id = ?", j) == "none"
    assert scalar("SELECT COUNT(*) FROM pitches WHERE job_id = ?", j) == 0



def test_llm_error_reaches_terminal_error_with_retry(client, scalar, monkeypatch):
    j = _ready_job()

    def boom(**kwargs):
        raise LLMError("The pitch writer is unavailable right now.")

    from app.services import pitch as PS

    monkeypatch.setattr(PS, "get_provider", lambda: type("X", (), {"extract": staticmethod(boom)})())
    pitch_service.run_pitch(j)
    assert scalar("SELECT pitch_status FROM jobs WHERE id = ?", j) == "error"
    assert scalar("SELECT pitch_error FROM jobs WHERE id = ?", j)
    tab = client.get(f"/app/jobs/{public_id_of('jobs', j)}/tab/pitch").text
    assert "Retry" in tab


def test_job_delete_cascades_pitches(client, scalar):
    j = _ready_job()
    client.post(f"/app/jobs/{public_id_of('jobs', j)}/pitch", headers={"hx-request": "true"})
    assert scalar("SELECT COUNT(*) FROM pitches WHERE job_id = ?", j) == 1
    J.delete_job(j, 1)
    assert scalar("SELECT COUNT(*) FROM pitches WHERE job_id = ?", j) == 0


def test_pitch_routes_404_for_other_users_job(client, scalar):
    j = _ready_job()
    pid = public_id_of("jobs", j)
    conn = get_conn()
    try:
        conn.execute("INSERT INTO users (id, name) VALUES (999, 'other') ON CONFLICT DO NOTHING")
        conn.execute("UPDATE jobs SET user_id = 999 WHERE id = ?", (j,))
        conn.commit()
    finally:
        conn.close()
    assert client.post(f"/app/jobs/{pid}/pitch").status_code == 404
    assert client.get(f"/app/jobs/{pid}/tab/pitch").status_code == 404
