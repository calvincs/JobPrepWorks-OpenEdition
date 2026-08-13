import json
import logging

from app.db import get_conn
from app.llm.base import LLMError, get_provider
from app.llm.prompts import JOB_EXTRACTION_SYSTEM, job_extraction_prompt
from app.models.extraction import JobExtraction
from app.services import analysis
from app.user_errors import USER_ERROR_GENERIC

log = logging.getLogger(__name__)


def canonical_skill(skill: str) -> str:
    return " ".join(skill.lower().split())


def create_job(
    raw_posting: str,
    *,
    user_id: int,
    source: str = "pasted",
    source_document_id: int | None = None,
    url: str | None = None,
) -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            """INSERT INTO jobs (user_id, raw_posting, source, source_document_id, url, busy_since)
               VALUES (?, ?, ?, ?, ?, datetime('now')) RETURNING id""",
            (user_id, raw_posting, source, source_document_id, url or None),
        )
        job_id = cur.fetchone()[0]
        conn.commit()
        return job_id
    finally:
        conn.close()


def run_intake(job_id: int) -> None:
    """Background pipeline for FR-2: extract structure, then trigger fit analysis (FR-3)."""
    conn = get_conn()
    try:
        row = conn.execute("SELECT raw_posting, user_id FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return
        user_id = row["user_id"]
        conn.execute(
            "UPDATE jobs SET extract_status = 'extracting', extract_error = NULL, "
            "busy_since = datetime('now') WHERE id = ?",
            (job_id,),
        )
        conn.commit()
    finally:
        conn.close()

    # The provider round-trip can take minutes — no pooled connection is held.
    try:
        result: JobExtraction = get_provider().extract(
            system=JOB_EXTRACTION_SYSTEM,
            prompt=job_extraction_prompt(row["raw_posting"]),
            schema=JobExtraction,
        )
    except LLMError as exc:
        log.warning("job %s extraction failed: %s", job_id, exc)
        conn = get_conn()
        try:
            conn.execute(
                "UPDATE jobs SET extract_status = 'error', extract_error = ? WHERE id = ?",
                (str(exc), job_id),  # LLMError copy is curated at the provider
            )
            conn.commit()
        finally:
            conn.close()
        return

    conn = get_conn()
    try:
        # The prompt is told to always produce a title; coalesce defensively
        # so a null from any provider still yields something displayable.
        title = (result.title or "").strip() or (
            f"{result.seniority.strip().title()} role" if result.seniority else "Untitled role"
        )
        conn.execute(
            """UPDATE jobs SET title = ?, company = ?, location = ?, pay_min = ?, pay_max = ?,
                    seniority = ?, sector = ?, responsibilities_json = ?, benefits_json = ?,
                    extract_status = 'ready'
               WHERE id = ?""",
            (
                title,
                result.company,
                result.location,
                result.pay_min,
                result.pay_max,
                result.seniority,
                result.sector,
                json.dumps(result.responsibilities),
                json.dumps(result.benefits),
                job_id,
            ),
        )
        conn.execute("DELETE FROM job_requirements WHERE job_id = ?", (job_id,))
        for req in result.requirements:
            conn.execute(
                """INSERT INTO job_requirements (job_id, kind, skill, skill_display, level, evidence_text)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (job_id, req.kind, canonical_skill(req.skill), req.skill, req.level, req.evidence_text),
            )
        conn.commit()
    except Exception:
        # Don't strand the row in 'extracting' (the UI would poll forever).
        log.exception("job %s intake write failed", job_id)
        conn.rollback()
        conn.execute(
            "UPDATE jobs SET extract_status = 'error', extract_error = ? WHERE id = ?",
            (USER_ERROR_GENERIC, job_id),
        )
        conn.commit()
        return
    finally:
        conn.close()

    # Company pulse (Pulse tab): cache-first — an existing pulse for this
    # employer is reused free; a new company submits research (metered daily).
    from app.services import pulse

    pulse.kickoff(result.company, user_id)

    analysis.run_fit_analysis(job_id)
    # Questions are no longer generated here — they're built per interview session
    # (at session start) from the job + the candidate's profile.

    from app.services import insights

    insights.request_refresh(user_id)  # FR-8: cross-job picture changed


def update_tracking(
    job_id: int,
    *,
    user_id: int,
    status: str,
    interest_level: int | None,
    interest_why: str | None,
    url: str | None,
    applied_at: str | None,
    outcome: str | None,
) -> None:
    from app.services import tracking

    conn = get_conn()
    try:
        old = conn.execute(
            "SELECT status FROM jobs WHERE id = ? AND user_id = ?", (job_id, user_id)
        ).fetchone()
        if old is None:  # missing or not owned — do nothing
            return
        conn.execute(
            """UPDATE jobs SET status = ?, interest_level = ?, interest_why = ?,
                    url = ?, applied_at = ?, outcome = ?
               WHERE id = ? AND user_id = ?""",
            (
                status,
                interest_level,
                interest_why or None,
                url or None,
                applied_at or None,
                outcome or None,
                job_id,
                user_id,
            ),
        )
        conn.commit()
        old_status = old["status"] if old else None
    finally:
        conn.close()

    if old_status is not None and old_status != status:
        tracking.log_event(job_id, "status_change", {"from": old_status, "to": status})
        if status == "applied":
            tracking.ensure_applied_follow_up(job_id)
        from app.services import insights

        insights.mark_stale(user_id)  # job status feeds the insights sector block


def delete_job(job_id: int, user_id: int) -> str | None:
    """Delete a job and everything associated with it. Returns a display name for
    the toast, or None if the job didn't exist or isn't owned by this user.

    FK cascades (get_conn sets foreign_keys = ON) remove job_requirements,
    fit_analyses, questions, interview_sessions (→ session_answers, assessments),
    study_guides, application_events and follow_ups. The uploaded posting
    document + its file on disk are NOT covered by the jobs cascade and are
    cleaned up explicitly (jobs.source_document_id → documents is ON DELETE SET
    NULL, the reverse direction, so deleting the job would otherwise orphan the
    document)."""
    from app.services.storage import get_storage

    conn = get_conn()
    try:
        job = conn.execute(
            "SELECT title, company, source_document_id FROM jobs WHERE id = ? AND user_id = ?",
            (job_id, user_id),
        ).fetchone()
        if job is None:  # missing or not owned by this user
            return None

        doc_path = None
        if job["source_document_id"] is not None:
            doc = conn.execute(
                "SELECT path FROM documents WHERE id = ? AND purpose = 'job'",
                (job["source_document_id"],),
            ).fetchone()
            if doc is not None:
                doc_path = doc["path"]

        conn.execute("DELETE FROM jobs WHERE id = ? AND user_id = ?", (job_id, user_id))
        # The posting document is used only by this job; remove it too.
        if job["source_document_id"] is not None:
            conn.execute(
                "DELETE FROM documents WHERE id = ? AND purpose = 'job'",
                (job["source_document_id"],),
            )
        conn.commit()
    finally:
        conn.close()

    if doc_path is not None:
        get_storage().delete(doc_path)

    return job["title"] or job["company"] or "Job"


def get_job(job_id: int, user_id: int):
    conn = get_conn()
    try:
        job = conn.execute(
            "SELECT * FROM jobs WHERE id = ? AND user_id = ?", (job_id, user_id)
        ).fetchone()
        if job is None:  # missing or not owned by this user
            return None, [], None
        requirements = conn.execute(
            "SELECT * FROM job_requirements WHERE job_id = ? ORDER BY kind, skill",
            (job_id,),
        ).fetchall()
        fit = conn.execute(
            "SELECT * FROM fit_analyses WHERE job_id = ? ORDER BY version DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        return job, requirements, fit
    finally:
        conn.close()


def posting_document(job_id: int):
    """The uploaded file behind a 'file'-sourced job (path/filename/mime), or None
    for a pasted job (or if the file was later removed). Ownership is enforced by
    the caller's route dependency."""
    conn = get_conn()
    try:
        return conn.execute(
            """SELECT d.path, d.filename, d.mime_type FROM documents d
               JOIN jobs j ON j.source_document_id = d.id
               WHERE j.id = ? AND d.purpose = 'job'""",
            (job_id,),
        ).fetchone()
    finally:
        conn.close()


def fit_history(job_id: int, user_id: int):
    conn = get_conn()
    try:
        return conn.execute(
            """SELECT f.* FROM fit_analyses f JOIN jobs j ON j.id = f.job_id
               WHERE f.job_id = ? AND j.user_id = ? ORDER BY f.version DESC""",
            (job_id, user_id),
        ).fetchall()
    finally:
        conn.close()


# Every jobs-table column is sortable. Keys are whitelisted (never interpolate
# a raw request value) and each maps to an ORDER BY built by _order_by().
SORT_COLUMNS = frozenset({"added", "title", "company", "status", "interest", "fit"})
DEFAULT_SORT = "added"

# List/row queries select every jobs column EXCEPT the heavy payloads
# (raw_posting and the *_json blobs) — dragging those into every table render
# and 3s row poll costs real transfer for data the templates never read there.
ROW_COLUMNS = (
    "j.id, j.user_id, j.title, j.company, j.location, j.pay_min, j.pay_max, "
    "j.seniority, j.sector, j.source, j.source_document_id, j.url, j.status, "
    "j.interest_level, j.interest_why, j.applied_at, j.outcome, "
    "j.extract_status, j.extract_error, j.analysis_status, j.analysis_error, "
    "j.study_status, j.study_error, j.created_at, j.public_id"
)

# Status sorts by pipeline stage, not alphabetically.
_STATUS_ORDER = (
    "CASE j.status WHEN 'researching' THEN 0 WHEN 'training' THEN 1 WHEN 'applied' THEN 2 "
    "WHEN 'interviewing' THEN 3 WHEN 'offer' THEN 4 WHEN 'rejected' THEN 5 "
    "WHEN 'withdrawn' THEN 6 ELSE 7 END"
)


def _order_by(sort: str, dir_sql: str) -> str:
    if sort in ("title", "company"):
        col = "j.title" if sort == "title" else "j.company"
        # Empty/null last; case-insensitive; stable by recency.
        return f"({col} IS NULL OR {col} = ''), LOWER({col}) {dir_sql}, j.created_at DESC"
    if sort == "status":
        return f"{_STATUS_ORDER} {dir_sql}, j.created_at DESC"
    if sort == "interest":  # unrated jobs always last
        return f"j.interest_level IS NULL, j.interest_level {dir_sql}, j.created_at DESC"
    if sort == "fit":  # un-analyzed jobs always last
        return f"fit_score IS NULL, fit_score {dir_sql}, j.created_at DESC"
    return f"j.created_at {dir_sql}"  # added / default


def list_jobs(sort: str = DEFAULT_SORT, direction: str = "desc", *, user_id: int,
              limit: int | None = None):
    """The jobs table, sorted by a whitelisted column. `limit` serves the
    dashboard's Recent-jobs card (top N under the chosen ordering — the
    secondary sort in _order_by keeps newest-first within ties)."""
    if sort not in SORT_COLUMNS:
        sort = DEFAULT_SORT
    dir_sql = "ASC" if direction == "asc" else "DESC"
    order = _order_by(sort, dir_sql)
    sql = f"""SELECT {ROW_COLUMNS},
                     (SELECT score FROM fit_analyses f
                      WHERE f.job_id = j.id ORDER BY version DESC LIMIT 1) AS fit_score,
                     (SELECT band FROM fit_analyses f
                      WHERE f.job_id = j.id ORDER BY version DESC LIMIT 1) AS fit_band
              FROM jobs j WHERE j.user_id = ?
              ORDER BY {order}"""
    params: tuple = (user_id,)
    if limit is not None:
        sql += " LIMIT ?"
        params = (user_id, int(limit))
    conn = get_conn()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def get_job_row(job_id: int, user_id: int):
    """One job with the same fit columns list_jobs provides (for row partials)."""
    conn = get_conn()
    try:
        return conn.execute(
            f"""SELECT {ROW_COLUMNS},
                      (SELECT score FROM fit_analyses f
                       WHERE f.job_id = j.id ORDER BY version DESC LIMIT 1) AS fit_score,
                      (SELECT band FROM fit_analyses f
                       WHERE f.job_id = j.id ORDER BY version DESC LIMIT 1) AS fit_band
               FROM jobs j WHERE j.id = ? AND j.user_id = ?""",
            (job_id, user_id),
        ).fetchone()
    finally:
        conn.close()
