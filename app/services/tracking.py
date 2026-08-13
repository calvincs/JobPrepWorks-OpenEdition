"""FR-9/FR-10: application event log and follow-up reminders."""

import json
from datetime import date, timedelta

from app.db import get_conn

STALE_DAYS = 14


def log_event(job_id: int, kind: str, payload: dict, occurred_at: str | None = None) -> None:
    owner = None
    conn = get_conn()
    try:
        if occurred_at:
            conn.execute(
                "INSERT INTO application_events (job_id, kind, payload_json, occurred_at) VALUES (?, ?, ?, ?)",
                (job_id, kind, json.dumps(payload), occurred_at),
            )
        else:
            conn.execute(
                "INSERT INTO application_events (job_id, kind, payload_json) VALUES (?, ?, ?)",
                (job_id, kind, json.dumps(payload)),
            )
        conn.commit()
        if kind == "feedback":
            owner = conn.execute(
                "SELECT user_id FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
    finally:
        conn.close()
    if owner is not None:
        from app.services import insights

        insights.mark_stale(owner["user_id"])  # feedback feeds the insights prompt


def list_events(job_id: int):
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM application_events WHERE job_id = ? ORDER BY occurred_at DESC, id DESC",
            (job_id,),
        ).fetchall()
    finally:
        conn.close()
    return [
        {**dict(r), "payload": json.loads(r["payload_json"])}
        for r in rows
    ]


def create_follow_up(job_id: int, due_at: str, reason: str) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO follow_ups (job_id, due_at, reason) VALUES (?, ?, ?)",
            (job_id, due_at, reason),
        )
        conn.commit()
    finally:
        conn.close()


def ensure_applied_follow_up(job_id: int) -> None:
    """FR-10: applying auto-suggests a follow-up if the job has no open one."""
    conn = get_conn()
    try:
        open_count = conn.execute(
            "SELECT COUNT(*) FROM follow_ups WHERE job_id = ? AND resolved_at IS NULL",
            (job_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    if open_count == 0:
        due = (date.today() + timedelta(days=STALE_DAYS)).isoformat()
        create_follow_up(
            job_id,
            due,
            f"No response {STALE_DAYS} days after applying — nudge, move on, or keep training?",
        )


def open_follow_ups(job_id: int | None = None, *, user_id: int):
    conn = get_conn()
    try:
        where = "WHERE f.resolved_at IS NULL AND j.user_id = ?"
        params: list = [user_id]
        if job_id is not None:
            where += " AND f.job_id = ?"
            params.append(job_id)
        return conn.execute(
            f"""SELECT f.*, j.public_id AS job_pid, j.title AS job_title,
                       j.company AS job_company, j.status AS job_status,
                       (f.due_at <= date('now')) AS due
                FROM follow_ups f JOIN jobs j ON j.id = f.job_id
                {where}
                ORDER BY f.due_at""",
            params,
        ).fetchall()
    finally:
        conn.close()


def resolve_follow_up(follow_up_id: int, resolution: str, user_id: int) -> None:
    conn = get_conn()
    try:
        row = conn.execute(
            """SELECT f.job_id, f.reason FROM follow_ups f JOIN jobs j ON j.id = f.job_id
               WHERE f.id = ? AND j.user_id = ?""",
            (follow_up_id, user_id),
        ).fetchone()
        if row is None:  # missing or not owned
            return
        conn.execute(
            "UPDATE follow_ups SET resolved_at = datetime('now'), resolution = ? WHERE id = ?",
            (resolution, follow_up_id),
        )
        conn.commit()
    finally:
        conn.close()
    log_event(row["job_id"], "note", {"text": f"Follow-up resolved: {resolution} ({row['reason']})"})


def snooze_follow_up(follow_up_id: int, days: int = 7, *, user_id: int) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE follow_ups SET due_at = date(due_at, ?) "
            "WHERE id = ? AND resolved_at IS NULL "
            "AND job_id IN (SELECT id FROM jobs WHERE user_id = ?)",
            (f"+{int(days)} days", follow_up_id, user_id),
        )
        conn.commit()
    finally:
        conn.close()
