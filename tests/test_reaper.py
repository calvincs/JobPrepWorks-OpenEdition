"""The stale-pipeline reaper (services/reaper.py): rows stranded in-flight by a
dead process flip to a retryable error; fresh in-flight rows (another server's
live work) survive."""

from app.db import get_conn
from app.services import reaper
from app.user_errors import USER_ERROR_INTERRUPTED

STALE = "2020-01-01 00:00:00"


def _execute(sql, *params):
    conn = get_conn()
    try:
        cur = conn.execute(sql, params)
        row = cur.fetchone() if "RETURNING" in sql else None
        conn.commit()
        return row[0] if row else None
    finally:
        conn.close()


def _job(**cols):
    keys = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    return _execute(
        f"INSERT INTO jobs (user_id, raw_posting, {keys}) VALUES (1, 'x', {marks}) RETURNING id",
        *cols.values(),
    )


def test_stale_jobs_rows_reaped_fresh_survive(scalar):
    stale = _job(extract_status="extracting", busy_since=STALE)
    unstamped = _job(extract_status="pending", busy_since=None)  # pre-migration shape
    fresh = _job(extract_status="extracting")  # live claim: stamped just now
    _execute("UPDATE jobs SET busy_since = datetime('now') WHERE id = ?", fresh)

    reaper.sweep()

    assert scalar("SELECT extract_status FROM jobs WHERE id = ?", stale) == "error"
    assert scalar("SELECT extract_error FROM jobs WHERE id = ?", stale) == USER_ERROR_INTERRUPTED
    assert scalar("SELECT extract_status FROM jobs WHERE id = ?", unstamped) == "error"
    assert scalar("SELECT extract_status FROM jobs WHERE id = ?", fresh) == "extracting"


def test_each_jobs_pipeline_column_reaped(scalar):
    job = _job(extract_status="ready", analysis_status="running", study_status="running",
               pitch_status="running", resume_status="running", busy_since=STALE)
    reaper.sweep()
    assert scalar("SELECT analysis_status FROM jobs WHERE id = ?", job) == "error"
    assert scalar("SELECT study_status FROM jobs WHERE id = ?", job) == "error"
    assert scalar("SELECT pitch_status FROM jobs WHERE id = ?", job) == "error"
    assert scalar("SELECT pitch_error FROM jobs WHERE id = ?", job) == USER_ERROR_INTERRUPTED
    assert scalar("SELECT resume_status FROM jobs WHERE id = ?", job) == "error"
    assert scalar("SELECT resume_error FROM jobs WHERE id = ?", job) == USER_ERROR_INTERRUPTED
    assert scalar("SELECT extract_status FROM jobs WHERE id = ?", job) == "ready"  # untouched


def test_stale_documents_and_sessions_reaped(scalar):
    doc = _execute(
        "INSERT INTO documents (user_id, purpose, filename, path, mime_type, status, busy_since) "
        "VALUES (1, 'profile', 'f.txt', '/tmp/f.txt', 'text/plain', 'parsing', ?) RETURNING id",
        STALE,
    )
    ses = _execute(
        "INSERT INTO interview_sessions (user_id, scope, setup_status, busy_since) "
        "VALUES (1, 'global', 'generating', ?) RETURNING id",
        STALE,
    )
    reaper.sweep()
    assert scalar("SELECT status FROM documents WHERE id = ?", doc) == "error"
    assert scalar("SELECT error FROM documents WHERE id = ?", doc) == USER_ERROR_INTERRUPTED
    assert scalar("SELECT setup_status FROM interview_sessions WHERE id = ?", ses) == "error"


def test_stale_answer_grades_reaped(scalar):
    ses = _execute(
        "INSERT INTO interview_sessions (user_id, scope, setup_status) "
        "VALUES (1, 'global', 'ready') RETURNING id"
    )
    q = _execute(
        "INSERT INTO questions (user_id, type, skill, skill_display, difficulty, text, "
        "ideal_answer_criteria) VALUES (1, 'technical', 's', 'S', 'easy', 't', 'c') RETURNING id"
    )
    ans = _execute(
        "INSERT INTO session_answers (session_id, question_id, position, grade_status, busy_since) "
        "VALUES (?, ?, 1, 'grading', ?) RETURNING id",
        ses, q, STALE,
    )
    reaper.sweep()
    assert scalar("SELECT grade_status FROM session_answers WHERE id = ?", ans) == "error"
    assert scalar("SELECT grade_error FROM session_answers WHERE id = ?", ans) == USER_ERROR_INTERRUPTED


def test_stale_fact_parses_reaped_fresh_survive(scalar):
    stale = _execute(
        "INSERT INTO fact_parses (user_id, raw_text, created_at) VALUES (1, 'x', ?) RETURNING id",
        STALE,
    )
    fresh = _execute("INSERT INTO fact_parses (user_id, raw_text) VALUES (1, 'y') RETURNING id")
    reaper.sweep()
    assert scalar("SELECT status FROM fact_parses WHERE id = ?", stale) == "error"
    assert scalar("SELECT status FROM fact_parses WHERE id = ?", fresh) == "running"
