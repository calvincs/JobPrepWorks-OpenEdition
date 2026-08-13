import json
import logging

from app import db as dberr

from app.db import get_conn
from app.llm.base import LLMError, get_provider
from app.llm.prompts import FIT_ANALYSIS_SYSTEM, fit_analysis_prompt
from app.models.extraction import FitAnalysisResult
from app.services.profile import (
    direction_block_for_prompt,
    has_profile_facts,
    profile_block_for_prompt,
)
from app.text import canonical
from app.user_errors import USER_ERROR_GENERIC, USER_ERROR_RACE

log = logging.getLogger(__name__)


def band_for_score(score: int) -> str:
    """SPEC FR-3 bands; thresholds mirror the score calibration in the fit prompt."""
    if score >= 80:
        return "Strong"
    if score >= 60:
        return "Viable"
    if score >= 40:
        return "Stretch"
    return "Weak"


def reanalyze_all_jobs(user_id: int) -> None:
    """Re-run fit analysis for every extraction-ready job. Triggered when a
    profile first appears, so jobs that were skipped (no profile) or scored
    against an empty profile get a real, evidence-backed fit. Runs sequentially;
    the polling row UI updates each job as its analysis lands."""
    from app.services import usage

    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id FROM jobs WHERE user_id = ? AND extract_status = 'ready'",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return
    # One ledger row of N units for the whole batch. This runs as a side effect
    # of the profile's first facts landing (already behind a metered action), so
    # a refusal skips quietly — each job's Fit tab still offers a manual run.
    if not usage.try_spend(user_id, "fit_all", units=len(rows)):
        log.warning("reanalyze_all_jobs skipped for user %s: daily LLM quota exhausted", user_id)
        return
    for r in rows:
        run_fit_analysis(r["id"])


def run_fit_analysis(job_id: int) -> None:
    """Compare job requirements against the profile; store a new versioned analysis (FR-3)."""
    conn = get_conn()
    try:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None or job["extract_status"] != "ready":
            return
        if not has_profile_facts(job["user_id"]):
            # Nothing to compare against — don't produce a misleading all-gaps
            # analysis. Leave status 'none'; the fit tab prompts for a profile.
            conn.execute(
                "UPDATE jobs SET analysis_status = 'none', analysis_error = NULL WHERE id = ?",
                (job_id,),
            )
            conn.commit()
            return
        requirements = conn.execute(
            "SELECT * FROM job_requirements WHERE job_id = ? ORDER BY kind, skill",
            (job_id,),
        ).fetchall()

        conn.execute(
            "UPDATE jobs SET analysis_status = 'running', analysis_error = NULL, "
            "busy_since = datetime('now') WHERE id = ?",
            (job_id,),
        )
        conn.commit()
    finally:
        conn.close()

    job_summary = " | ".join(
        str(v)
        for v in (job["title"], job["company"], job["seniority"], job["sector"], job["location"])
        if v
    )
    requirements_block = "\n".join(
        f"- ({r['kind']}) {r['skill_display']}"
        + (f" [{r['level']}]" if r["level"] else "")
        + (f' — "{r["evidence_text"]}"' if r["evidence_text"] else "")
        for r in requirements
    )

    direction_block = direction_block_for_prompt(job["user_id"])

    # The provider round-trip can take minutes — no pooled connection is held.
    try:
        result: FitAnalysisResult = get_provider().extract(
            system=FIT_ANALYSIS_SYSTEM,
            prompt=fit_analysis_prompt(
                job_summary, requirements_block,
                profile_block_for_prompt(job["user_id"]), direction_block,
            ),
            schema=FitAnalysisResult,
        )
    except LLMError as exc:
        log.warning("fit analysis for job %s failed: %s", job_id, exc)
        conn = get_conn()
        try:
            conn.execute(
                "UPDATE jobs SET analysis_status = 'error', analysis_error = ? WHERE id = ?",
                (str(exc), job_id),  # LLMError copy is curated at the provider
            )
            conn.commit()
        finally:
            conn.close()
        return

    conn = get_conn()
    try:
        try:
            # Server-side gate: alignment is stored only when direction facts
            # actually fed the prompt — the model (and the mock, which always
            # returns a canned alignment) can't smuggle one in without them.
            alignment_json = (
                json.dumps(result.alignment.model_dump())
                if (direction_block and result.alignment)
                else None
            )
            payload = (
                result.score,
                band_for_score(result.score),
                json.dumps([s.model_dump() for s in result.strengths]),
                json.dumps([g.model_dump() for g in result.gaps]),
                json.dumps([a.model_dump() for a in result.study_areas]),
                alignment_json,
            )
            for _ in range(5):
                version = conn.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 FROM fit_analyses WHERE job_id = ?",
                    (job_id,),
                ).fetchone()[0]
                try:
                    conn.execute(
                        """INSERT INTO fit_analyses
                           (job_id, version, score, band, strengths_json, gaps_json,
                            study_areas_json, alignment_json)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (job_id, version, *payload),
                    )
                    break
                except dberr.ForeignKeyViolation:
                    conn.rollback()  # job deleted mid-analysis — nothing to store
                    return
                except dberr.UniqueViolation:
                    conn.rollback()  # version race with a concurrent analysis; retry
                    continue
            else:
                # Every attempt lost the version race — surface as an error rather
                # than strand the row in 'running' polling forever (A10).
                log.warning("fit analysis for job %s exhausted version retries", job_id)
                conn.rollback()
                conn.execute(
                    "UPDATE jobs SET analysis_status = 'error', analysis_error = ? WHERE id = ?",
                    (USER_ERROR_RACE, job_id),
                )
                conn.commit()
                return
            conn.execute("UPDATE jobs SET analysis_status = 'ready' WHERE id = ?", (job_id,))
            conn.commit()
        except Exception:
            # Any unexpected failure must reach a terminal 'error' status.
            log.exception("fit analysis for job %s failed unexpectedly", job_id)
            conn.rollback()
            conn.execute(
                "UPDATE jobs SET analysis_status = 'error', analysis_error = ? WHERE id = ?",
                (USER_ERROR_GENERIC, job_id),
            )
            conn.commit()
            return
    finally:
        conn.close()

    from app.services import gamification, insights

    gamification.check_awards(job["user_id"])  # a re-analysis may earn 'fit_improved'
    insights.mark_stale(job["user_id"])  # fit scores feed the insights sector block


def resolve_gap(job_id: int, requirement: str, *, user_id: int) -> bool:
    """Record that the user addressed a gap ("I have this" → fact created)
    against the job's LATEST fit analysis. The requirement must actually be a
    gap of that analysis (canonical match) — anything else is refused, which
    bounds the table and blocks junk from a tampered form. Idempotent via the
    UNIQUE constraint + ON CONFLICT DO NOTHING; returns True when a resolution
    exists after the call (a double-add still refreshes the UI), False when
    there is no analysis, no such gap, or the job isn't the user's."""
    key = canonical(requirement or "")
    if not key:
        return False
    conn = get_conn()
    try:
        fit = conn.execute(
            """SELECT f.id, f.gaps_json FROM fit_analyses f
               JOIN jobs j ON j.id = f.job_id
               WHERE f.job_id = ? AND j.user_id = ?
               ORDER BY f.version DESC LIMIT 1""",
            (job_id, user_id),
        ).fetchone()
        if fit is None:
            return False
        if key not in {canonical(g["requirement"]) for g in json.loads(fit["gaps_json"])}:
            return False
        conn.execute(
            "INSERT INTO fit_gap_resolutions (fit_analysis_id, requirement_key) "
            "VALUES (?, ?) ON CONFLICT DO NOTHING",
            (fit["id"], key),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def resolved_gap_keys(fit_analysis_id: int, *, user_id: int) -> set[str]:
    """Canonical requirement keys the user has marked addressed on this
    analysis (drives the Fit tab's checked-off gaps). Owner-scoped through
    jobs like every fit_analyses read."""
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT r.requirement_key FROM fit_gap_resolutions r
               JOIN fit_analyses f ON f.id = r.fit_analysis_id
               JOIN jobs j ON j.id = f.job_id
               WHERE r.fit_analysis_id = ? AND j.user_id = ?""",
            (fit_analysis_id, user_id),
        ).fetchall()
        return {r["requirement_key"] for r in rows}
    finally:
        conn.close()
