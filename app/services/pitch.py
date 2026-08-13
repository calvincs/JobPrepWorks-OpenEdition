"""Your Pitch: per-job "tell me about yourself" variants (15s/30s/2min +
talking points), grounded in evidenced profile facts, the job's requirements,
the latest fit analysis, and the user's stated career direction. Mirrors the
fit-analysis pipeline: claim → close conn → LLM → reopen → versioned insert."""

import json
import logging

from app import db as dberr

from app.db import get_conn
from app.llm.base import LLMError, get_provider
from app.llm.prompts import PITCH_SYSTEM, pitch_prompt
from app.models.extraction import PitchResult
from app.services.profile import (
    direction_block_for_prompt,
    has_profile_facts,
    profile_block_for_prompt,
)
from app.user_errors import USER_ERROR_GENERIC, USER_ERROR_RACE

log = logging.getLogger(__name__)


def latest_pitch(job_id: int):
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT * FROM pitches WHERE job_id = ? ORDER BY version DESC LIMIT 1",
            (job_id,),
        ).fetchone()
    finally:
        conn.close()


def _fit_block(fit) -> str:
    """Compact plaintext of the latest fit analysis for the pitch prompt."""
    if fit is None:
        return ""
    lines = [f"score: {fit['score']}/100 ({fit['band']})"]
    for s in json.loads(fit["strengths_json"]):
        lines.append(f"strength: {s['requirement']} — {s['evidence']}")
    for g in json.loads(fit["gaps_json"]):
        lines.append(f"gap ({g['importance']}): {g['requirement']} — {g['why']}")
    return "\n".join(lines)


def run_pitch(job_id: int) -> None:
    """Write a new versioned pitch for the job (background task)."""
    conn = get_conn()
    try:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None or job["extract_status"] != "ready":
            return
        if not has_profile_facts(job["user_id"]):
            # Nothing evidenced to speak from — don't fabricate a pitch.
            # Leave status 'none'; the tab prompts for a profile.
            conn.execute(
                "UPDATE jobs SET pitch_status = 'none', pitch_error = NULL WHERE id = ?",
                (job_id,),
            )
            conn.commit()
            return
        requirements = conn.execute(
            "SELECT * FROM job_requirements WHERE job_id = ? ORDER BY kind, skill",
            (job_id,),
        ).fetchall()
        fit = conn.execute(
            "SELECT * FROM fit_analyses WHERE job_id = ? ORDER BY version DESC LIMIT 1",
            (job_id,),
        ).fetchone()

        conn.execute(
            "UPDATE jobs SET pitch_status = 'running', pitch_error = NULL, "
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

    # The provider round-trip can take minutes — no pooled connection is held.
    try:
        result: PitchResult = get_provider().extract(
            system=PITCH_SYSTEM,
            prompt=pitch_prompt(
                job_summary,
                requirements_block,
                profile_block_for_prompt(job["user_id"]),
                _fit_block(fit),
                direction_block_for_prompt(job["user_id"]),
            ),
            schema=PitchResult,
        )
    except LLMError as exc:
        log.warning("pitch for job %s failed: %s", job_id, exc)
        conn = get_conn()
        try:
            conn.execute(
                "UPDATE jobs SET pitch_status = 'error', pitch_error = ? WHERE id = ?",
                (str(exc), job_id),  # LLMError copy is curated at the provider
            )
            conn.commit()
        finally:
            conn.close()
        return

    conn = get_conn()
    try:
        try:
            content_json = result.model_dump_json()
            for _ in range(5):
                version = conn.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 FROM pitches WHERE job_id = ?",
                    (job_id,),
                ).fetchone()[0]
                try:
                    conn.execute(
                        "INSERT INTO pitches (job_id, version, content_json) VALUES (?, ?, ?)",
                        (job_id, version, content_json),
                    )
                    break
                except dberr.ForeignKeyViolation:
                    conn.rollback()  # job deleted mid-run — nothing to store
                    return
                except dberr.UniqueViolation:
                    conn.rollback()  # version race with a concurrent run; retry
                    continue
            else:
                log.warning("pitch for job %s exhausted version retries", job_id)
                conn.rollback()
                conn.execute(
                    "UPDATE jobs SET pitch_status = 'error', pitch_error = ? WHERE id = ?",
                    (USER_ERROR_RACE, job_id),
                )
                conn.commit()
                return
            conn.execute("UPDATE jobs SET pitch_status = 'ready' WHERE id = ?", (job_id,))
            conn.commit()
        except Exception:
            # Any unexpected failure must reach a terminal 'error' status.
            log.exception("pitch for job %s failed unexpectedly", job_id)
            conn.rollback()
            conn.execute(
                "UPDATE jobs SET pitch_status = 'error', pitch_error = ? WHERE id = ?",
                (USER_ERROR_GENERIC, job_id),
            )
            conn.commit()
            return
    finally:
        conn.close()
