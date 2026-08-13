"""Opaque public ids make URLs non-enumerable; internal integer ids never leak."""

from app.db import get_conn, public_id_of, resolve_id
from app.services import jobs as J
from app.services import profile as P
from app.services import tracking as T

ZERO_UUID = "00000000-0000-0000-0000-000000000000"


def test_sequential_int_paths_404(client):
    J.create_job("Backend role", user_id=1)  # internal id 1 now exists
    # A sequential integer is not a valid uuid -> can't be used to address a job.
    assert client.get("/app/jobs/1").status_code == 404
    assert client.post("/app/jobs/1/delete").status_code == 404
    assert client.post("/app/interviews/2/finish").status_code == 404
    # A well-formed but unknown uuid also 404s (no enumeration foothold).
    assert client.get(f"/app/jobs/{ZERO_UUID}").status_code == 404


def test_job_list_links_use_public_id_not_int(client):
    j = J.create_job("Backend role", user_id=1)
    J.run_intake(j)
    pid = public_id_of("jobs", j)
    html = client.get("/app/jobs").text
    assert f"/app/jobs/{pid}" in html  # links use the opaque uuid
    assert f'/jobs/{j}"' not in html and f"/app/jobs/{j}/" not in html  # never the int
    assert client.get(f"/app/jobs/{pid}").status_code == 200


def test_public_id_resolution_is_owner_scoped():
    j = J.create_job("Backend role", user_id=1)
    pid = public_id_of("jobs", j)
    assert resolve_id("jobs", pid, where="AND user_id = ?", params=(1,)) == j
    assert resolve_id("jobs", pid, where="AND user_id = ?", params=(999,)) is None
    assert resolve_id("jobs", "not-a-uuid") is None


def test_start_interview_accepts_public_job_id(client, scalar):
    P.create_manual_fact(user_id=1, kind="skill", name="Python")
    j = J.create_job("Backend role", user_id=1)
    J.run_intake(j)
    r = client.post(
        "/app/interviews",
        data={"scope": "job", "job_id": public_id_of("jobs", j), "count": 5},
        follow_redirects=False,
    )
    location = r.headers["location"]
    assert location.startswith("/app/interviews/") and "-" in location  # redirect carries a uuid
    assert scalar("SELECT COUNT(*) FROM interview_sessions") == 1


def test_follow_up_uses_public_id(client, scalar):
    j = J.create_job("Backend role", user_id=1)
    J.run_intake(j)
    T.create_follow_up(j, "2026-09-01", "check in")
    fu_pid = scalar("SELECT public_id FROM follow_ups LIMIT 1")
    assert client.post("/app/followups/1/snooze").status_code == 404  # int rejected
    assert client.post(f"/app/followups/{fu_pid}/snooze").status_code == 200
    assert scalar("SELECT due_at FROM follow_ups LIMIT 1") == "2026-09-08"


def test_insight_dismiss_uses_public_id(client, scalar):
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO insights (user_id, kind, title, body) VALUES (1, 'gap', 'T', 'B')"
        )
        conn.commit()
    finally:
        conn.close()
    i_pid = scalar("SELECT public_id FROM insights LIMIT 1")
    assert client.post("/app/insights/1/dismiss").status_code == 404  # int rejected
    assert client.post(f"/app/insights/{i_pid}/dismiss").status_code == 200
    assert scalar("SELECT dismissed FROM insights LIMIT 1") == 1
