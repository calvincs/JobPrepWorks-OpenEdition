"""Stale-pipeline reaper: crash recovery for the async LLM pipelines.

Every long pipeline claims its row by flipping a status column to an in-flight
value ('extracting', 'running', 'grading', …), stamps `busy_since`, and does
the work in an in-process BackgroundTask. In-process exceptions are caught and
flip the row to a terminal 'error' — but a killed process (deploy, OOM,
scale-in) can't, and the HTMX partial would poll the stranded row forever.
sweep() flips any in-flight row whose liveness stamp is older than
REAPER_STALE_MINUTES to 'error' with retryable copy. Company Pulse keeps its
own recovery (pulse.sweep() — claim heartbeat + stale-claim requeue) and is
deliberately not handled here.

Multi-server safe: each statement is one atomic conditional UPDATE — the
status predicate is re-checked under the row lock, so a still-live pipeline's
terminal write either beats the reap or lands after it (a reaped row a live
pipeline later finishes just becomes ready — harmless). Runs at boot and every
REAPER_INTERVAL seconds on every server (main.py, same pattern as
_quota_sweeper); tests set REAPER_INTERVAL=0 and call sweep() directly.
"""

import logging
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.db import get_conn
from app.user_errors import USER_ERROR_INTERRUPTED

log = logging.getLogger(__name__)

# (table, status column, in-flight values, error-copy column). All of these
# tables carry `busy_since`, stamped by every claim (and by the INSERTs that
# create rows already in-flight). A NULL stamp (pre-migration row) is treated
# as stale — one-time, retryable.
_PIPELINES = [
    ("jobs", "extract_status", ("pending", "extracting"), "extract_error"),
    ("jobs", "analysis_status", ("running",), "analysis_error"),
    ("jobs", "study_status", ("running",), "study_error"),
    ("jobs", "pitch_status", ("running",), "pitch_error"),
    ("jobs", "resume_status", ("running",), "resume_error"),
    ("documents", "status", ("uploaded", "parsing", "extracting"), "error"),
    ("interview_sessions", "setup_status", ("generating",), "setup_error"),
    ("interview_sessions", "assessment_status", ("running",), "assessment_error"),
    ("session_answers", "grade_status", ("grading",), "grade_error"),
    ("session_answers", "followup_status", ("grading",), "followup_error"),
]


def _cutoff() -> str:
    stale = timedelta(minutes=settings.reaper_stale_minutes)
    return (datetime.now(timezone.utc) - stale).strftime("%Y-%m-%d %H:%M:%S")


def sweep() -> None:
    """Flip stale in-flight pipeline rows to terminal 'error' (retryable)."""
    cutoff = _cutoff()
    conn = get_conn()
    try:
        for table, status_col, in_flight, error_col in _PIPELINES:
            marks = ", ".join("?" for _ in in_flight)
            cur = conn.execute(
                f"""UPDATE {table} SET {status_col} = 'error', {error_col} = ?,
                           busy_since = NULL
                    WHERE {status_col} IN ({marks})
                      AND (busy_since IS NULL OR busy_since <= ?)""",
                (USER_ERROR_INTERRUPTED, *in_flight, cutoff),
            )
            if cur.rowcount:
                log.warning("reaped %d stale %s.%s row(s)", cur.rowcount, table, status_col)
        # fact_parses rows are created already-running; created_at is the stamp.
        cur = conn.execute(
            """UPDATE fact_parses SET status = 'error', error = ?
                WHERE status = 'running' AND created_at <= ?""",
            (USER_ERROR_INTERRUPTED, cutoff),
        )
        if cur.rowcount:
            log.warning("reaped %d stale fact_parses row(s)", cur.rowcount)
        # insight_runs: started_at is the stamp; finished_at closes the run.
        cur = conn.execute(
            """UPDATE insight_runs SET status = 'error', error = ?,
                      finished_at = datetime('now')
                WHERE status = 'running' AND started_at <= ?""",
            (USER_ERROR_INTERRUPTED, cutoff),
        )
        if cur.rowcount:
            log.warning("reaped %d stale insight_runs row(s)", cur.rowcount)
        conn.commit()
    finally:
        conn.close()
