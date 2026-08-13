"""Ownership scoping (IDOR): a different user must never reach another's data."""

from app.routers.search import _run_search
from app.services import interviews as I
from app.services import jobs as J
from app.services import profile as P

OTHER = 999  # a user who owns nothing


def test_job_access_scoped_to_owner():
    j = J.create_job("Backend role", user_id=1)
    assert J.get_job(j, user_id=1)[0] is not None
    assert J.get_job(j, user_id=OTHER)[0] is None
    assert J.get_job_row(j, user_id=OTHER) is None
    assert J.delete_job(j, user_id=OTHER) is None  # non-owned delete is a no-op
    assert J.get_job(j, user_id=1)[0] is not None  # still there


def test_session_access_scoped_to_owner():
    P.create_manual_fact(user_id=1, kind="skill", name="Python")
    j = J.create_job("Backend role", user_id=1); J.run_intake(j)
    sid = I.create_session("job", [j], 5, user_id=1)
    assert I.get_session(sid, user_id=OTHER)[0] is None
    assert I.delete_session(sid, user_id=OTHER) is False
    assert I.get_session(sid, user_id=1)[0] is not None


def test_fact_access_scoped_to_owner():
    fid, _ = P.create_manual_fact(kind="skill", name="Python", user_id=1)
    assert P.fact_with_sources(fid, user_id=OTHER) is None
    P.delete_fact(fid, user_id=OTHER)  # no-op
    assert P.fact_with_sources(fid, user_id=1) is not None


def test_search_is_user_scoped():
    P.create_manual_fact(kind="skill", name="Kubernetes", user_id=1)
    owner_results, owner_total = _run_search("kubernetes", user_id=1)
    assert len(owner_results["facts"]) >= 1
    _, other_total = _run_search("kubernetes", user_id=OTHER)
    assert other_total == 0


def test_job_action_route_404s_for_missing_job(client):
    assert client.post("/app/jobs/99999/analyze").status_code == 404


def test_session_action_route_404s_for_missing_session(client):
    assert client.post("/app/interviews/99999/finish").status_code == 404
