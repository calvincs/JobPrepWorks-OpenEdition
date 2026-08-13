"""FR-11 gamification core: mastery trends, streaks, session stats, awards.

Everything except badges is derived from session/answer/fit data at read time.
The game layer rewards effort and improvement - scores themselves stay
calibrated (FR-6)."""

import json
from datetime import date, timedelta

from app.db import get_conn

AWARD_DEFS = {
    "first_job": "First job analyzed",
    "first_session": "First interview completed",
    "sessions_10": "10 interviews completed",
    "first_perfect": "First 5/5 answer",
    "streak_3": "3-day interview streak",
    "streak_7": "7-day interview streak",
    "fit_improved": "Fit score improved",
    "gap_closed": "Closed a gap",
}


def mastery(user_id: int, limit: int | None = None):
    """Per-skill rolling average across all graded answers, weakest first. A
    graded follow-up supersedes the initial score (COALESCE)."""
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT q.skill, MAX(q.skill_display) AS skill_display,
                      ROUND(AVG(COALESCE(a.followup_score, a.score)), 1) AS avg_score, COUNT(*) AS n
               FROM session_answers a JOIN questions q ON q.id = a.question_id
               WHERE a.score IS NOT NULL AND q.user_id = ?
               GROUP BY q.skill
               ORDER BY avg_score ASC, n DESC""",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    return rows[:limit] if limit else rows


def completed_session_dates(user_id: int) -> set[date]:
    conn = get_conn()
    try:
        # completed_at is 'YYYY-MM-DD HH:MM:SS' UTC; the date portion is its day.
        rows = conn.execute(
            """SELECT DISTINCT substr(completed_at, 1, 10) AS d FROM interview_sessions
               WHERE user_id = ? AND status = 'completed' AND completed_at IS NOT NULL""",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    return {date.fromisoformat(r["d"]) for r in rows}


def current_streak(user_id: int) -> int:
    """Consecutive days with >= 1 completed session. A streak survives until a
    full day is missed - today without a session yet doesn't break it."""
    dates = completed_session_dates(user_id)
    if not dates:
        return 0
    day = date.today()
    if day not in dates:
        day -= timedelta(days=1)
        if day not in dates:
            return 0
    streak = 0
    while day in dates:
        streak += 1
        day -= timedelta(days=1)
    return streak


def session_stats(user_id: int) -> dict:
    conn = get_conn()
    try:
        completed = conn.execute(
            "SELECT COUNT(*) FROM interview_sessions WHERE user_id = ? AND status = 'completed'",
            (user_id,),
        ).fetchone()[0]
        answered = conn.execute(
            """SELECT COUNT(*) FROM session_answers a
               JOIN interview_sessions s ON s.id = a.session_id
               WHERE s.user_id = ? AND a.answer_text IS NOT NULL""",
            (user_id,),
        ).fetchone()[0]
        best = conn.execute(
            # Follow-up is final: COALESCE(followup_score, score) per answer.
            """SELECT MAX(avg_score) FROM (
                 SELECT AVG(COALESCE(a.followup_score, a.score)) AS avg_score FROM session_answers a
                 JOIN interview_sessions s ON s.id = a.session_id
                 WHERE s.user_id = ? AND a.score IS NOT NULL
                 GROUP BY a.session_id HAVING COUNT(a.score) >= 3)""",
            (user_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    return {
        "sessions_completed": completed,
        "questions_answered": answered,
        "best_session_avg": round(best, 1) if best is not None else None,
    }


def recent_awards(user_id: int, limit: int = 4):
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT * FROM awards WHERE user_id = ? ORDER BY earned_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    finally:
        conn.close()


def _gap_closed(conn, user_id: int) -> bool:
    """True if any requirement moved from the gap list to the strength list
    between consecutive fit-analysis versions of the same job."""
    rows = conn.execute(
        """SELECT f.job_id, f.version, f.strengths_json, f.gaps_json
           FROM fit_analyses f JOIN jobs j ON j.id = f.job_id
           WHERE j.user_id = ?
           ORDER BY f.job_id, f.version""",
        (user_id,),
    ).fetchall()
    prev_by_job: dict[int, set[str]] = {}
    for r in rows:
        gaps_now = {g["requirement"].lower() for g in json.loads(r["gaps_json"])}
        strengths_now = {s["requirement"].lower() for s in json.loads(r["strengths_json"])}
        prev_gaps = prev_by_job.get(r["job_id"])
        if prev_gaps and prev_gaps & strengths_now:
            return True
        prev_by_job[r["job_id"]] = gaps_now
    return False


def check_awards(user_id: int) -> list[str]:
    """Evaluate unearned badges against current data; award what now qualifies."""
    conn = get_conn()
    try:
        earned = {
            r["kind"]
            for r in conn.execute(
                "SELECT kind FROM awards WHERE user_id = ?", (user_id,)
            )
        }
        newly: list[str] = []

        def qualify(kind: str, condition: bool, meta: dict | None = None):
            if kind in earned or not condition:
                return
            conn.execute(
                "INSERT INTO awards (user_id, kind, title, meta_json) VALUES (?, ?, ?, ?) "
                "ON CONFLICT DO NOTHING",
                (user_id, kind, AWARD_DEFS[kind], json.dumps(meta) if meta else None),
            )
            newly.append(kind)

        completed = conn.execute(
            "SELECT COUNT(*) FROM interview_sessions WHERE user_id = ? AND status = 'completed'",
            (user_id,),
        ).fetchone()[0]
        qualify("first_session", completed >= 1)
        qualify("sessions_10", completed >= 10)

        perfect = conn.execute(
            """SELECT COUNT(*) FROM session_answers a
               JOIN interview_sessions s ON s.id = a.session_id
               WHERE s.user_id = ? AND a.score = 5""",
            (user_id,),
        ).fetchone()[0]
        qualify("first_perfect", perfect >= 1)

        improved = conn.execute(
            """SELECT 1 FROM fit_analyses a
               JOIN fit_analyses b ON a.job_id = b.job_id AND a.version > b.version
               JOIN jobs j ON j.id = a.job_id
               WHERE j.user_id = ? AND a.score > b.score LIMIT 1""",
            (user_id,),
        ).fetchone()
        qualify("fit_improved", improved is not None)

        analyzed = conn.execute(
            """SELECT COUNT(*) FROM fit_analyses f JOIN jobs j ON j.id = f.job_id
               WHERE j.user_id = ?""",
            (user_id,),
        ).fetchone()[0]
        qualify("first_job", analyzed >= 1)

        if "gap_closed" not in earned:
            qualify("gap_closed", _gap_closed(conn, user_id))

        conn.commit()
    finally:
        conn.close()

    streak = current_streak(user_id)
    conn = get_conn()
    try:
        for kind, needed in (("streak_3", 3), ("streak_7", 7)):
            if streak >= needed:
                conn.execute(
                    "INSERT INTO awards (user_id, kind, title) VALUES (?, ?, ?) "
                    "ON CONFLICT DO NOTHING",
                    (user_id, kind, AWARD_DEFS[kind]),
                )
        conn.commit()
    finally:
        conn.close()
    return newly
