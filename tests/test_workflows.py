"""Tracking, study guides, insights, and page-render smoke workflows."""

from app.db import public_id_of
from app.services import jobs as J
from app.services import profile as P
from app.services import study as study_service
from app.services import tracking as tracking_service


def _job():
    j = J.create_job("Backend role", user_id=1)
    J.run_intake(j)
    return j


def test_tracking_status_change_logs_event_and_follow_up(scalar):
    j = _job()
    J.update_tracking(
        j, user_id=1, status="applied", interest_level=4, interest_why="great team",
        url=None, applied_at="2026-07-01", outcome=None,
    )
    assert scalar("SELECT status FROM jobs WHERE id = ?", j) == "applied"
    # status change is logged, and applying schedules a follow-up
    assert scalar(
        "SELECT COUNT(*) FROM application_events WHERE job_id = ? AND kind = 'status_change'", j
    ) == 1
    assert scalar("SELECT COUNT(*) FROM follow_ups WHERE job_id = ?", j) >= 1


def test_follow_up_create_snooze_resolve(scalar):
    j = _job()
    tracking_service.create_follow_up(j, "2026-08-01", "check in")
    fu = tracking_service.open_follow_ups(j, user_id=1)[0]
    assert fu["reason"] == "check in"

    tracking_service.snooze_follow_up(fu["id"], days=7, user_id=1)  # date arithmetic (make_interval)
    assert scalar("SELECT due_at FROM follow_ups WHERE id = ?", fu["id"]) == "2026-08-08"

    tracking_service.resolve_follow_up(fu["id"], "done", 1)
    assert tracking_service.open_follow_ups(j, user_id=1) == []


def test_notes_and_feedback_events(scalar):
    j = _job()
    tracking_service.log_event(j, "note", {"text": "phone screen went well"})
    tracking_service.log_event(j, "feedback", {"text": "improve system design"})
    kinds = {e["kind"] for e in tracking_service.list_events(j)}
    assert {"note", "feedback"} <= kinds


def test_study_guide_generation(scalar):
    P.create_manual_fact(user_id=1, kind="skill", name="Python")
    j = _job()
    study_service.generate_guide(j)
    assert scalar("SELECT COUNT(*) FROM study_guides WHERE job_id = ?", j) == 1
    assert study_service.latest_guide(j) is not None


def test_dashboard_and_insights_render(client):
    P.create_manual_fact(user_id=1, kind="skill", name="Python")
    _job()
    assert client.get("/app").status_code == 200
    assert client.get("/app/insights").status_code == 200


def test_job_activity_tab_renders(client):
    j = _job()
    assert client.get(f"/app/jobs/{public_id_of('jobs', j)}?tab=activity").status_code == 200
