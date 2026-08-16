import json

from app import db as dberr

from app.db import get_conn
from app.llm.base import get_provider
from app.llm.prompts import (
    QUESTION_GENERATION_SYSTEM,
    STUDY_DRILL_SYSTEM,
    question_generation_prompt,
    study_drill_prompt,
)
from app.models.extraction import QuestionBank
from app.services.profile import profile_block_for_prompt

def _canonical(skill: str) -> str:
    return " ".join(skill.lower().split())


def _job_context(conn, job_id: int):
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
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
        gaps_block = "\n".join(f"- [{g['importance']}] {g['requirement']}: {g['why']}" for g in gaps)
    return job, job_summary, requirements_block, gaps_block


def generate_for_session(job_id: int, count: int, *, user_id: int) -> list[int]:
    """Generate `count` fresh questions for one job at interview time, tailored by
    the job's requirements, the candidate's profile, and any known fit gaps. Past
    questions for the job are passed so the model avoids repeats. Inserts them
    (source 'session') and returns the new question ids. Raises LLMError on
    failure; returns [] if the job isn't ready to generate against."""
    conn = get_conn()
    try:
        job, job_summary, requirements_block, gaps_block = _job_context(conn, job_id)
        if job is None or job["extract_status"] != "ready":
            return []
        existing = conn.execute(
            "SELECT text FROM questions WHERE job_id = ? ORDER BY id", (job_id,)
        ).fetchall()
        existing_block = "\n".join(f"- {r['text']}" for r in existing)
    finally:
        conn.close()

    # The provider round-trip can take minutes — no pooled connection is held.
    result: QuestionBank = get_provider().extract(
        system=QUESTION_GENERATION_SYSTEM,
        prompt=question_generation_prompt(
            job_summary,
            requirements_block,
            profile_block_for_prompt(user_id),
            gaps_block,
            existing_block,
            count,
        ),
        schema=QuestionBank,
    )

    conn = get_conn()
    try:
        ids: list[int] = []
        try:
            # The schema doesn't bound how many questions the model returns;
            # `count` is the contract, so anything extra is dropped here.
            for q in result.questions[:count]:
                cur = conn.execute(
                    """INSERT INTO questions
                       (user_id, job_id, type, skill, skill_display, difficulty, text,
                        ideal_answer_criteria, source)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'session') RETURNING id""",
                    (
                        user_id,
                        job_id,
                        q.type,
                        _canonical(q.skill),
                        q.skill,
                        q.difficulty,
                        q.text,
                        q.ideal_answer_criteria,
                    ),
                )
                ids.append(cur.fetchone()[0])
            conn.commit()
        except dberr.IntegrityError:
            conn.rollback()  # job deleted mid-generation — build_session handles []
            return []
        return ids
    finally:
        conn.close()


def create_opener_question(job_id: int, *, user_id: int) -> int | None:
    """The 'tell me about yourself' session opener — deterministic, no LLM.
    The value is in the grading criteria, which the existing grade pipeline
    consumes verbatim: they demand a narrative aimed at THIS job's must-haves,
    not a generic life story. Returns None if the job isn't ready."""
    conn = get_conn()
    try:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None or job["extract_status"] != "ready":
            return None
        musts = conn.execute(
            "SELECT skill_display FROM job_requirements WHERE job_id = ? AND kind = 'must' ORDER BY skill",
            (job_id,),
        ).fetchall()
        must_names = ", ".join(r["skill_display"] for r in musts) or "the role's core requirements"
        role = " at ".join(str(v) for v in (job["title"], job["company"]) if v)
        criteria = (
            "A strong answer is a 60-90 second first-person narrative, not a resume "
            "recitation. It must explicitly connect the candidate's background to this "
            f"role{f' ({role})' if role else ''} and its must-have requirements — "
            f"{must_names} — citing concrete experience for at least the most important "
            "of them; follow a present -> past -> why-this-role arc; and end on why "
            "this specific job. Generic life stories or unconnected chronology should "
            "score 3 or below."
        )
        try:
            cur = conn.execute(
                """INSERT INTO questions
                   (user_id, job_id, type, skill, skill_display, difficulty, text,
                    ideal_answer_criteria, source)
                   VALUES (?, ?, 'behavioral', 'introduction', 'Introduction', 'medium',
                           'To start us off — tell me about yourself.', ?, 'session')
                   RETURNING id""",
                (user_id, job_id, criteria),
            )
            row = cur.fetchone()
            conn.commit()
            return row[0]
        except dberr.IntegrityError:
            conn.rollback()  # job deleted mid-build — the session fails cleanly later
            return None
    finally:
        conn.close()


def generate_for_topic(
    job_id: int | None, topic: str, why_it_matters: str, how_it_will_be_tested: str,
    *, user_id: int,
) -> int | None:
    """Generate ONE question drilling a single study-guide topic. Used by impromptu
    study drills. With a job_id the question is tailored to that role; with job_id
    None (a Global-focus-plan drill) it's general practice from the topic + the
    candidate's profile. Inserts it and returns the new question id, or None if the
    named job isn't ready. Raises LLMError on failure."""
    if job_id is None:
        job_summary = "(general practice across the candidate's target roles — not one specific job)"
    else:
        conn = get_conn()
        try:
            job, job_summary, _requirements_block, _gaps_block = _job_context(conn, job_id)
        finally:
            conn.close()
        if job is None or job["extract_status"] != "ready":
            return None

    # The provider round-trip can take minutes — no pooled connection is held.
    result: QuestionBank = get_provider().extract(
        system=STUDY_DRILL_SYSTEM,
        prompt=study_drill_prompt(
            job_summary,
            profile_block_for_prompt(user_id),
            topic,
            why_it_matters,
            how_it_will_be_tested,
        ),
        schema=QuestionBank,
    )
    if not result.questions:
        return None
    q = result.questions[0]
    conn = get_conn()
    try:
        try:
            cur = conn.execute(
                """INSERT INTO questions
                   (user_id, job_id, type, skill, skill_display, difficulty, text,
                    ideal_answer_criteria, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'session') RETURNING id""",
                (
                    user_id,
                    job_id,
                    q.type,
                    _canonical(q.skill),
                    q.skill,
                    q.difficulty,
                    q.text,
                    q.ideal_answer_criteria,
                ),
            )
            qid = cur.fetchone()[0]
            conn.commit()
        except dberr.IntegrityError:
            conn.rollback()  # job deleted mid-generation
            return None
        return qid
    finally:
        conn.close()
