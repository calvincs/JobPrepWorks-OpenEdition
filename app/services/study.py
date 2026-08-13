import json
import logging

from app import db as dberr

from app.db import get_conn, set_state
from app.llm.base import LLMError, get_provider
from app.llm.prompts import STUDY_GUIDE_SYSTEM, study_guide_prompt
from app.models.extraction import StudyGuideResult
from app.services.profile import profile_block_for_prompt
from app.user_errors import USER_ERROR_GENERIC, USER_ERROR_RACE

log = logging.getLogger(__name__)


def global_status_key(user_id: int) -> str:
    """app_state key for the user's global-guide status. Per-user: the guide
    data is per-user (study_guides.user_id), so its status must be too."""
    return f"global_study_status:{user_id}"


def global_error_key(user_id: int) -> str:
    return f"global_study_error:{user_id}"


def _feedback_block(conn, user_id: int, job_id: int | None = None) -> str:
    where = "WHERE e.kind = 'feedback' AND j.user_id = ?"
    params: list = [user_id]
    if job_id is not None:
        where += " AND e.job_id = ?"
        params.append(job_id)
    rows = conn.execute(
        f"""SELECT e.payload_json, j.title FROM application_events e
            JOIN jobs j ON j.id = e.job_id {where}
            ORDER BY e.occurred_at DESC LIMIT 10""",
        params,
    ).fetchall()
    return "\n".join(
        f"- {r['title'] or 'job'}: {json.loads(r['payload_json']).get('text', '')}" for r in rows
    )


def generate_guide(job_id: int) -> None:
    """Background pipeline for FR-7: versioned per-job study guide, prioritized by
    fit gaps and interview performance."""
    from app.services.interviews import skill_performance

    conn = get_conn()
    try:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None or job["extract_status"] != "ready":
            return
        user_id = job["user_id"]
        conn.execute(
            "UPDATE jobs SET study_status = 'running', study_error = NULL, "
            "busy_since = datetime('now') WHERE id = ?", (job_id,)
        )
        conn.commit()

        requirements = conn.execute(
            "SELECT * FROM job_requirements WHERE job_id = ? ORDER BY kind, skill", (job_id,)
        ).fetchall()
        fit = conn.execute(
            "SELECT * FROM fit_analyses WHERE job_id = ? ORDER BY version DESC LIMIT 1", (job_id,)
        ).fetchone()

        job_summary = " | ".join(
            str(v)
            for v in (job["title"], job["company"], job["seniority"], job["sector"], job["location"])
            if v
        )
        requirements_block = "\n".join(
            f"- ({r['kind']}) {r['skill_display']}" + (f" [{r['level']}]" if r["level"] else "")
            for r in requirements
        )
        gaps_block = ""
        if fit:
            gaps = json.loads(fit["gaps_json"])
            gaps_block = "\n".join(
                f"- [{g['importance']}] {g['requirement']}: {g['why']}" for g in gaps
            )
        perf = skill_performance(conn, user_id)
        performance_block = "\n".join(
            f"- {skill}: avg {avg:.1f}/5 over {n} answer(s)" for skill, (avg, n) in sorted(perf.items())
        )
        feedback_block = _feedback_block(conn, user_id, job_id)
    finally:
        conn.close()

    # The provider round-trip can take minutes — no pooled connection is held.
    try:
        result: StudyGuideResult = get_provider().extract(
            system=STUDY_GUIDE_SYSTEM,
            prompt=study_guide_prompt(
                job_summary,
                requirements_block,
                profile_block_for_prompt(user_id),
                gaps_block,
                performance_block,
                feedback_block=feedback_block,
            ),
            schema=StudyGuideResult,
        )
    except LLMError as exc:
        log.warning("study guide for job %s failed: %s", job_id, exc)
        conn = get_conn()
        try:
            conn.execute(
                "UPDATE jobs SET study_status = 'error', study_error = ? WHERE id = ?",
                (str(exc), job_id),  # LLMError copy is curated at the provider
            )
            conn.commit()
        finally:
            conn.close()
        return

    conn = get_conn()
    try:
        try:
            content = result.model_dump_json()
            for _ in range(5):
                version = conn.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 FROM study_guides WHERE job_id = ?",
                    (job_id,),
                ).fetchone()[0]
                try:
                    conn.execute(
                        "INSERT INTO study_guides (user_id, job_id, version, content_json) VALUES (?, ?, ?, ?)",
                        (user_id, job_id, version, content),
                    )
                    break
                except dberr.ForeignKeyViolation:
                    conn.rollback()  # job deleted mid-generation
                    return
                except dberr.UniqueViolation:
                    conn.rollback()  # version race — retry
                    continue
            else:
                log.warning("study guide for job %s exhausted version retries", job_id)
                conn.rollback()
                conn.execute(
                    "UPDATE jobs SET study_status = 'error', study_error = ? WHERE id = ?",
                    (USER_ERROR_RACE, job_id),
                )
                conn.commit()
                return
            conn.execute("UPDATE jobs SET study_status = 'ready' WHERE id = ?", (job_id,))
            conn.commit()
        except Exception:
            log.exception("study guide for job %s failed unexpectedly", job_id)
            conn.rollback()
            conn.execute(
                "UPDATE jobs SET study_status = 'error', study_error = ? WHERE id = ?",
                (USER_ERROR_GENERIC, job_id),
            )
            conn.commit()
            return
    finally:
        conn.close()


def latest_guide(job_id: int):
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT * FROM study_guides WHERE job_id = ? ORDER BY version DESC LIMIT 1", (job_id,)
        ).fetchone()
    finally:
        conn.close()


def _topic_from_guide(guide, topic_name: str) -> tuple[str, str]:
    if guide is None:
        return "", ""
    for t in json.loads(guide["content_json"]).get("topics", []):
        if t.get("topic") == topic_name:
            return t.get("why_it_matters", ""), t.get("how_it_will_be_tested", "")
    return "", ""


def topic_detail(job_id: int | None, topic_name: str, *, user_id: int) -> tuple[str, str]:
    """Best-effort (why_it_matters, how_it_will_be_tested) for a topic — used to
    re-steer a 'Practice another' drill on the same topic. Reads the job's guide
    for a per-job drill, or the global focus plan for a job-less one. Returns
    ('', '') if the guide or topic is gone."""
    guide = latest_guide(job_id) if job_id is not None else latest_global_guide(user_id)
    return _topic_from_guide(guide, topic_name)


def switcher_jobs(user_id: int) -> list:
    """Every extract-ready job for the /study hub switcher, flagged by whether it
    already has a study guide (`has_guide`). Jobs still extracting can't have a
    guide yet, so they're left out — there'd be nothing to generate against.
    Most recently added first."""
    conn = get_conn()
    try:
        return conn.execute(
            """SELECT j.id, j.public_id, j.title, j.company,
                      EXISTS (SELECT 1 FROM study_guides g WHERE g.job_id = j.id) AS has_guide
               FROM jobs j
               WHERE j.user_id = ? AND j.extract_status = 'ready'
               ORDER BY j.created_at DESC""",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()


def mark_guide_generating(job_id: int, user_id: int) -> bool:
    """Flip an owned, extract-ready job's guide to 'running' up front so the page
    can render the polling state immediately, before the background task starts.
    Returns False when the job isn't eligible (missing or not extracted yet)."""
    conn = get_conn()
    try:
        job = conn.execute(
            "SELECT extract_status FROM jobs WHERE id = ? AND user_id = ?", (job_id, user_id)
        ).fetchone()
        if job is None or job["extract_status"] != "ready":
            return False
        conn.execute(
            "UPDATE jobs SET study_status = 'running', study_error = NULL, "
            "busy_since = datetime('now') WHERE id = ?", (job_id,)
        )
        conn.commit()
        return True
    finally:
        conn.close()


def topic_drill_stats(job_id: int | None, user_id: int) -> dict:
    """Per-topic drill history for a study guide, keyed by topic label, so each
    topic card can show its progress. For every topic the user has drilled:
    `count` graded drills, the `scores` oldest→newest (a trend), and the
    cumulative-average `avg` confidence score. A drill's score is its effective
    answer score — a graded follow-up supersedes the initial one. `job_id` None
    covers Global-focus-plan drills (job_id IS NULL)."""
    conn = get_conn()
    try:
        job_clause = "s.job_id IS NULL" if job_id is None else "s.job_id = ?"
        params = (user_id,) if job_id is None else (user_id, job_id)
        rows = conn.execute(
            f"""SELECT s.label AS topic, COALESCE(a.followup_score, a.score) AS score
                FROM interview_sessions s
                JOIN session_answers a ON a.session_id = s.id
                WHERE s.user_id = ? AND s.scope = 'study' AND s.label IS NOT NULL
                  AND a.score IS NOT NULL AND {job_clause}
                ORDER BY s.started_at, a.id""",
            params,
        ).fetchall()
    finally:
        conn.close()
    by_topic: dict[str, list[int]] = {}
    for r in rows:
        by_topic.setdefault(r["topic"], []).append(r["score"])
    return {
        topic: {"count": len(scores), "scores": scores,
                "avg": round(sum(scores) / len(scores), 1)}
        for topic, scores in by_topic.items()
    }


def generate_global_guide(user_id: int) -> None:
    """FR-7: global guide synthesizing the highest-impact focus areas across all
    of one user's open applications. Stored with job_id NULL; status tracked in
    app_state under per-user keys."""
    from app.services.interviews import skill_performance

    set_state(global_status_key(user_id), "running")
    set_state(global_error_key(user_id), None)
    conn = get_conn()
    try:
        agg = conn.execute(
            """SELECT r.skill, MAX(r.skill_display) AS display,
                      COUNT(DISTINCT r.job_id) AS jobs_requiring,
                      SUM(CASE WHEN r.kind = 'must' THEN 1 ELSE 0 END) AS must_count
               FROM job_requirements r JOIN jobs j ON j.id = r.job_id
               WHERE j.user_id = ?
               GROUP BY r.skill ORDER BY jobs_requiring DESC, must_count DESC""",
            (user_id,),
        ).fetchall()
        if not agg:
            set_state(global_status_key(user_id), "none")
            return
        requirements_block = "\n".join(
            f"- {r['display']}: required by {r['jobs_requiring']} job(s), {r['must_count']} as must-have"
            for r in agg
        )
        gaps: list[str] = []
        for job in conn.execute(
            "SELECT id, title FROM jobs WHERE user_id = ? AND extract_status = 'ready'",
            (user_id,),
        ).fetchall():
            fit = conn.execute(
                "SELECT gaps_json FROM fit_analyses WHERE job_id = ? ORDER BY version DESC LIMIT 1",
                (job["id"],),
            ).fetchone()
            if fit:
                for g in json.loads(fit["gaps_json"]):
                    gaps.append(f"- ({job['title'] or 'job'}) [{g['importance']}] {g['requirement']}: {g['why']}")
        perf = skill_performance(conn, user_id)
        performance_block = "\n".join(
            f"- {skill}: avg {avg:.1f}/5 over {n} answer(s)" for skill, (avg, n) in sorted(perf.items())
        )
        feedback_block = _feedback_block(conn, user_id)
    finally:
        conn.close()

    try:
        result: StudyGuideResult = get_provider().extract(
            system=STUDY_GUIDE_SYSTEM,
            prompt=study_guide_prompt(
                "ALL TARGET JOBS COMBINED — find the common, highest-impact focus areas",
                requirements_block,
                profile_block_for_prompt(user_id),
                "\n".join(gaps),
                performance_block,
                feedback_block=feedback_block,
            ),
            schema=StudyGuideResult,
        )
    except LLMError as exc:
        log.warning("global study guide failed: %s", exc)
        set_state(global_status_key(user_id), "error")
        set_state(global_error_key(user_id), str(exc))  # LLMError copy is curated at the provider
        return

    conn = get_conn()
    try:
        content = result.model_dump_json()
        for _ in range(5):
            version = conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM study_guides WHERE job_id IS NULL AND user_id = ?",
                (user_id,),
            ).fetchone()[0]
            try:
                conn.execute(
                    "INSERT INTO study_guides (user_id, job_id, version, content_json) VALUES (?, NULL, ?, ?)",
                    (user_id, version, content),
                )
                break
            except dberr.UniqueViolation:
                conn.rollback()  # version race with another global-guide write — retry
                continue
        else:
            log.warning("global study guide exhausted version retries")
            conn.rollback()
            set_state(global_status_key(user_id), "error")
            set_state(global_error_key(user_id), USER_ERROR_RACE)
            return
        conn.commit()
    except Exception:
        log.exception("global study guide write failed")
        conn.rollback()
        set_state(global_status_key(user_id), "error")
        set_state(global_error_key(user_id), USER_ERROR_GENERIC)
        return
    finally:
        conn.close()
    set_state(global_status_key(user_id), "ready")


def latest_global_guide(user_id: int):
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT * FROM study_guides WHERE job_id IS NULL AND user_id = ? ORDER BY version DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    finally:
        conn.close()
