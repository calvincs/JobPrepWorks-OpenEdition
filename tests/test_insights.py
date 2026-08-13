"""Insights pipeline: run lifecycle, staleness marking, dismissal
stickiness, claim exclusivity, crash recovery, pruning, and ownership scoping."""

from starlette.testclient import TestClient

from app.db import get_conn
from app.main import app
from app.services import insights as I
from app.services import jobs as J
from app.services import profile as P
from app.services import tracking as T

# Canned mock-provider titles (see app/llm/mock_provider.py CANNED['InsightsResult'])
GAP_TITLE = "AWS depth is your biggest cross-job blocker"


def _job():
    j = J.create_job("Backend role", user_id=1)
    J.run_intake(j)  # ends with request_refresh -> a ready insight run
    return j


def _execute(sql, *params):
    conn = get_conn()
    try:
        cur = conn.execute(sql, params)
        row = cur.fetchone() if cur.description else None
        conn.commit()
        return row
    finally:
        conn.close()


def test_intake_generates_ready_run(scalar):
    _job()
    assert scalar("SELECT COUNT(*) FROM insight_runs WHERE user_id = 1 AND status = 'ready'") == 1
    assert scalar("SELECT COUNT(*) FROM insights WHERE user_id = 1") == 3  # canned set
    assert scalar(
        "SELECT COUNT(*) FROM insights WHERE run_id IS NULL OR canonical_title IS NULL"
    ) == 0
    state = I.page_status(1)
    assert state["status"] == "ready"
    assert state["analyzed_at"] is not None
    assert not state["stale"]


def test_refresh_replaces_current_set_and_carries_created_at(client, scalar):
    _job()
    first_run = scalar("SELECT MAX(id) FROM insight_runs WHERE status = 'ready'")
    _execute(
        "UPDATE insights SET created_at = '2026-01-01 00:00:00' WHERE title = ?", GAP_TITLE
    )
    client.post("/app/insights/refresh")

    assert I.page_status(1)["status"] == "ready"
    assert scalar("SELECT MAX(id) FROM insight_runs WHERE status = 'ready'") > first_run
    current = I.list_insights(1)
    assert len(current) == 3  # replaced, not appended
    gap = next(r for r in current if r["title"] == GAP_TITLE)
    # same (kind, canonical title) as the previous run -> created_at carried forward
    assert gap["created_at"] == "2026-01-01 00:00:00"


def test_feedback_marks_stale_and_refresh_clears(client, scalar):
    j = _job()
    assert not I.page_status(1)["stale"]

    T.log_event(j, "feedback", {"text": "more system design depth"})
    assert I.page_status(1)["stale"]
    assert "New insights are waiting" in client.get("/app/insights").text

    runs_before = scalar("SELECT COUNT(*) FROM insight_runs")
    client.post("/app/insights/refresh")
    assert scalar("SELECT COUNT(*) FROM insight_runs") == runs_before + 1
    assert not I.page_status(1)["stale"]
    after = client.get("/app/insights/list").text
    assert "New insights are waiting" not in after
    assert "analyzed" in after


def test_input_changes_mark_stale_without_generating(scalar):
    j = _job()
    runs = scalar("SELECT COUNT(*) FROM insight_runs")

    P.create_manual_fact(user_id=1, kind="skill", name="Terraform")  # profile evidence changed
    assert I.page_status(1)["stale"]
    J.update_tracking(
        j, user_id=1, status="applied", interest_level=None, interest_why=None,
        url=None, applied_at=None, outcome=None,
    )  # job status changed
    # staleness is a banner, not an LLM call: no new run appeared
    assert scalar("SELECT COUNT(*) FROM insight_runs") == runs
    assert I.page_status(1)["stale"]


def test_dismissed_insight_stays_gone_after_refresh(client, scalar):
    _job()
    pid = scalar("SELECT public_id FROM insights WHERE title = ?", GAP_TITLE)
    assert client.post(f"/app/insights/{pid}/dismiss").status_code == 200

    client.post("/app/insights/refresh")  # mock returns the identical canned set

    titles = [r["title"] for r in I.list_insights(1)]
    assert GAP_TITLE not in titles
    assert len(titles) == 2
    # the dismissed row survives as the suppression record
    assert scalar(
        "SELECT COUNT(*) FROM insights WHERE dismissed = 1 AND title = ?", GAP_TITLE
    ) == 1


def test_claim_is_exclusive_and_expires_dead_runs(scalar):
    _execute("INSERT INTO insight_runs (user_id) VALUES (1)")
    assert I.claim_run(1) is None  # a live running row holds the claim
    I.request_refresh(1)  # must bail out quietly
    assert scalar("SELECT COUNT(*) FROM insights") == 0

    # a running row older than the expiry window is a dead worker: expire + claim
    _execute("UPDATE insight_runs SET started_at = '2020-01-01 00:00:00'")
    run_id = I.claim_run(1)
    assert run_id is not None
    assert scalar(
        "SELECT status FROM insight_runs WHERE started_at = '2020-01-01 00:00:00'"
    ) == "error"
    assert scalar("SELECT status FROM insight_runs WHERE id = ?", run_id) == "running"


def test_reaper_recovers_interrupted_runs_but_spares_live_ones(scalar):
    from app.services import reaper

    # A fresh 'running' row (another server's live run) must survive a sweep —
    # the old boot-time recovery flipped ALL running rows and would have killed it.
    _execute("INSERT INTO insight_runs (user_id) VALUES (1)")
    reaper.sweep()
    assert scalar("SELECT status FROM insight_runs") == "running"
    # A stale one is a dead worker: reaped to a retryable error.
    _execute("UPDATE insight_runs SET started_at = '2020-01-01 00:00:00'")
    reaper.sweep()
    assert scalar("SELECT status FROM insight_runs") == "error"
    assert scalar("SELECT finished_at FROM insight_runs") is not None


def test_empty_matrix_still_finishes_ready(client, scalar):
    I.request_refresh(1)  # no jobs at all
    assert I.page_status(1)["status"] == "ready"
    assert scalar("SELECT COUNT(*) FROM insights") == 0
    assert "No insights yet" in client.get("/app/insights").text


def test_runs_pruned_to_cap_dismissed_rows_survive(client, scalar):
    _job()
    pid = scalar("SELECT public_id FROM insights WHERE title = ?", GAP_TITLE)
    client.post(f"/app/insights/{pid}/dismiss")

    for _ in range(12):
        I.request_refresh(1)

    assert scalar("SELECT COUNT(*) FROM insight_runs WHERE user_id = 1") == 10
    # the dismissed row's run was pruned; the row itself survives detached
    assert scalar("SELECT COUNT(*) FROM insights WHERE dismissed = 1 AND run_id IS NULL") == 1
    assert GAP_TITLE not in [r["title"] for r in I.list_insights(1)]  # still suppressed


def test_other_users_data_never_leaks(scalar):
    _job()  # user 1's canned requirements include 'aws'
    conn = get_conn()
    try:
        conn.execute("INSERT INTO users (id, name) VALUES (2, 'other')")
        sid = conn.execute(
            "INSERT INTO interview_sessions (user_id, scope, status) "
            "VALUES (2, 'global', 'completed') RETURNING id"
        ).fetchone()[0]
        qid = conn.execute(
            """INSERT INTO questions (user_id, type, skill, skill_display, difficulty,
                                      text, ideal_answer_criteria, source)
               VALUES (2, 'technical', 'aws', 'aws', 'medium', 'q', 'c', 'session')
               RETURNING id"""
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO session_answers (session_id, question_id, position, answer_text,
                                            score, grade_status)
               VALUES (?, ?, 1, 'a', 5, 'ready')""",
            (sid, qid),
        )
        conn.execute(
            "INSERT INTO profile_facts (user_id, kind, name, user_edited) "
            "VALUES (2, 'skill', 'AWS', 1)"
        )
        conn.commit()
        matrix = I._skill_matrix(conn, 1)
    finally:
        conn.close()

    aws_line = next(line for line in matrix.splitlines() if line.startswith("- aws"))
    assert "never practiced" in aws_line  # user 2's 5/5 answer doesn't count
    assert "profile evidence: NO" in aws_line  # user 2's fact isn't user 1's evidence
    assert I.list_insights(2) == []
    assert I.page_status(2)["status"] == "none"


def test_evidence_matcher_word_boundaries():
    conn = get_conn()
    try:
        for name in ("JavaScript", "C++ programming", "Machine  Learning models"):
            conn.execute(
                "INSERT INTO profile_facts (user_id, kind, name, user_edited) "
                "VALUES (1, 'skill', ?, 1)",
                (name,),
            )
        conn.commit()
        has = I._skill_evidence(conn, 1)
    finally:
        conn.close()

    assert has("javascript")
    assert not has("java")  # substring of javascript must NOT count
    assert has("c++")
    assert has("machine learning")  # canonical() collapses the double space
    assert not has("rust")
