import json
import logging

from app import db as dberr

from app.db import get_conn
from app.llm.base import LLMError, get_provider
from app.llm.prompts import (
    ANSWER_GRADING_SYSTEM,
    FOLLOWUP_SYSTEM,
    SESSION_ASSESSMENT_SYSTEM,
    answer_grading_prompt,
    followup_prompt,
    session_assessment_prompt,
)
from app.models.extraction import AnswerGrade, FollowUpQuestion, SessionAssessment
from app.services import gamification, insights, questions, study
from app.user_errors import USER_ERROR_GENERIC

log = logging.getLogger(__name__)

DIFFICULTY_RANK = {"easy": 0, "medium": 1, "hard": 2}


def _mark_terminal_error(sql: str, params: tuple) -> None:
    """Flip an in-flight pipeline row to a terminal 'error' on an unexpected
    failure so the polling UI stops (A10). Callers guard the UPDATE with the
    in-flight status (e.g. AND grade_status = 'grading') so a row that already
    reached a success terminal is never clobbered. Opens its own connection —
    the pipeline's own connection may be in an aborted transaction."""
    conn = get_conn()
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()

def fail_grade(answer_id: int, message: str) -> None:
    """Terminal-error an answer's grade without running it (e.g. the daily AI
    quota refused the grading task after the answer text was already saved).
    The feedback partial shows the message with the usual retry button."""
    _mark_terminal_error(
        "UPDATE session_answers SET grade_status = 'error', grade_error = ? "
        "WHERE id = ? AND grade_status = 'grading'",
        (message, answer_id),
    )


def fail_followup_grade(answer_id: int, message: str) -> None:
    """fail_grade for the follow-up answer's grade."""
    _mark_terminal_error(
        "UPDATE session_answers SET followup_status = 'error', followup_error = ? "
        "WHERE id = ? AND followup_status = 'grading'",
        (message, answer_id),
    )


def fail_assessment(session_id: int, message: str) -> None:
    """Terminal-error a finished session's assessment without running it — the
    session still completes; the assessment panel offers a retry."""
    _mark_terminal_error(
        "UPDATE interview_sessions SET assessment_status = 'error', assessment_error = ? "
        "WHERE id = ? AND assessment_status = 'running'",
        (message, session_id),
    )


# Answers scoring at or below this (out of 5) earn one probing follow-up question,
# giving the candidate a second chance to reach the specifics they missed.
FOLLOWUP_THRESHOLD = 3


def skill_performance(conn, user_id: int) -> dict[str, tuple[float, int]]:
    """Rolling average score and answer count per canonical skill (FR-11 mastery
    data). A graded follow-up is the candidate's final answer, so it supersedes
    the initial score (COALESCE)."""
    rows = conn.execute(
        """SELECT q.skill, AVG(COALESCE(a.followup_score, a.score)) AS avg_score, COUNT(*) AS n
           FROM session_answers a
           JOIN questions q ON q.id = a.question_id
           WHERE a.score IS NOT NULL AND q.user_id = ?
           GROUP BY q.skill""",
        (user_id,),
    ).fetchall()
    return {r["skill"]: (r["avg_score"], r["n"]) for r in rows}


def interviewable_jobs(conn=None, *, user_id: int) -> list:
    """Jobs ready to be interviewed: extraction finished and at least one
    requirement to generate questions from. Questions are built at session start,
    so a job no longer needs a pre-made bank to appear here."""
    own = conn is None
    if own:
        conn = get_conn()
    try:
        return conn.execute(
            """SELECT id, public_id, title, company, status FROM jobs
               WHERE user_id = ? AND extract_status = 'ready'
                 AND id IN (SELECT DISTINCT job_id FROM job_requirements)
               ORDER BY created_at DESC""",
            (user_id,),
        ).fetchall()
    finally:
        if own:
            conn.close()


def create_session(
    scope: str, job_ids: list[int], count: int = 10, *, user_id: int
) -> int | None:
    """Create an interview session over one or more jobs. Questions aren't built
    here — the session starts in setup_status 'generating' and build_session()
    fills it in the background. Returns the session id, or None if no selected
    job is interviewable."""
    conn = get_conn()
    try:
        interviewable = {r["id"] for r in interviewable_jobs(conn, user_id=user_id)}
        if scope == "global":
            job_ids = list(interviewable)
        job_ids = [jid for jid in job_ids if jid in interviewable]
        if not job_ids:
            return None
        cur = conn.execute(
            """INSERT INTO interview_sessions (user_id, scope, job_id, mixer_job_ids_json, setup_status, busy_since)
               VALUES (?, ?, ?, ?, 'generating', datetime('now')) RETURNING id""",
            (
                user_id,
                scope,
                job_ids[0] if scope == "job" else None,
                json.dumps(job_ids) if scope != "job" else None,
            ),
        )
        session_id = cur.fetchone()[0]
        conn.commit()
        return session_id
    finally:
        conn.close()


def create_study_drill(
    job_id: int | None, label: str, user_id: int
) -> int | None:
    """Create a one-question 'study' session drilling a single study-guide topic.
    A job_id ties it to a role (per-job guide); job_id None is a general drill from
    the Global focus plan. The question is built in the background by
    build_study_drill(). Returns the session id, or None if a named job isn't
    interviewable for this user."""
    conn = get_conn()
    try:
        if job_id is not None and job_id not in {r["id"] for r in interviewable_jobs(conn, user_id=user_id)}:
            return None
        cur = conn.execute(
            """INSERT INTO interview_sessions (user_id, scope, job_id, label, setup_status, busy_since)
               VALUES (?, 'study', ?, ?, 'generating', datetime('now')) RETURNING id""",
            (user_id, job_id, label),
        )
        session_id = cur.fetchone()[0]
        conn.commit()
        return session_id
    finally:
        conn.close()


def build_study_drill(
    session_id: int, job_id: int | None, topic: str, why_it_matters: str, how_it_will_be_tested: str
) -> None:
    """Background: generate the single topic-focused question for a study drill and
    lay it out as one answerable slot. Sets setup_status ready/error for polling."""
    try:
        _build_study_drill(session_id, job_id, topic, why_it_matters, how_it_will_be_tested)
    except Exception:  # never strand setup_status at 'generating' (A10)
        log.exception("study drill %s setup failed unexpectedly", session_id)
        _mark_terminal_error(
            "UPDATE interview_sessions SET setup_status = 'error', setup_error = ? "
            "WHERE id = ? AND setup_status = 'generating'",
            (USER_ERROR_GENERIC, session_id),
        )


def _build_study_drill(
    session_id: int, job_id: int | None, topic: str, why_it_matters: str, how_it_will_be_tested: str
) -> None:
    conn = get_conn()
    try:
        owner = conn.execute(
            "SELECT user_id FROM interview_sessions WHERE id = ?", (session_id,)
        ).fetchone()
    finally:
        conn.close()
    if owner is None:  # session deleted before the build started
        return
    try:
        qid = questions.generate_for_topic(
            job_id, topic, why_it_matters, how_it_will_be_tested, user_id=owner["user_id"]
        )
    except LLMError as exc:
        _fail_setup(session_id, str(exc))
        return
    if qid is None:
        _fail_setup(session_id, "Couldn't generate a practice question for this topic.")
        return

    conn = get_conn()
    try:
        conn.execute("DELETE FROM session_answers WHERE session_id = ?", (session_id,))
        conn.execute(
            "INSERT INTO session_answers (session_id, question_id, position) VALUES (?, ?, 1)",
            (session_id, qid),
        )
        conn.execute(
            "UPDATE interview_sessions SET setup_status = 'ready', setup_error = NULL WHERE id = ?",
            (session_id,),
        )
        conn.commit()
    except dberr.IntegrityError:
        conn.rollback()
        _fail_setup(session_id, "This drill's job was changed or removed while it was building.")
    finally:
        conn.close()


def _session_job_ids(session) -> list[int]:
    if session["scope"] == "job":
        return [session["job_id"]] if session["job_id"] else []
    return [j for j in json.loads(session["mixer_job_ids_json"] or "[]") if j]


def _distribute(count: int, n: int) -> list[int]:
    """Split `count` questions across `n` jobs as evenly as possible."""
    base, extra = divmod(count, n)
    return [base + (1 if i < extra else 0) for i in range(n)]


def build_session(session_id: int, count: int = 10, include_opener: bool = False) -> None:
    """Background: generate this session's questions (per job, tailored by the
    profile) and lay them out as answerable slots. Sets setup_status ready/error
    for the polling UI. Round-robins across jobs so multi-job sessions stay varied.
    include_opener (job-scope only) prepends a graded 'tell me about yourself'
    opener in place of one generated question, so `count` stays the total."""
    try:
        _build_session(session_id, count, include_opener)
    except Exception:  # never strand setup_status at 'generating' (A10)
        log.exception("session %s setup failed unexpectedly", session_id)
        _mark_terminal_error(
            "UPDATE interview_sessions SET setup_status = 'error', setup_error = ? "
            "WHERE id = ? AND setup_status = 'generating'",
            (USER_ERROR_GENERIC, session_id),
        )


def _build_session(session_id: int, count: int = 10, include_opener: bool = False) -> None:
    conn = get_conn()
    try:
        session = conn.execute(
            "SELECT * FROM interview_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if session is None:
            return
        job_ids = _session_job_ids(session)
    finally:
        conn.close()

    if not job_ids:
        _fail_setup(session_id, "This session has no jobs to draw questions from.")
        return

    # The opener replaces one generated question (count stays the total).
    opener_slots = 1 if (include_opener and session["scope"] == "job") else 0
    per_job = _distribute(max(count - opener_slots, len(job_ids)), len(job_ids))
    try:
        per_job_ids = [
            questions.generate_for_session(jid, n, user_id=session["user_id"])
            for jid, n in zip(job_ids, per_job)
        ]
    except LLMError as exc:
        _fail_setup(session_id, str(exc))
        return

    # Round-robin merge so consecutive questions come from different jobs.
    ordered: list[int] = []
    for tier in range(max((len(x) for x in per_job_ids), default=0)):
        for ids in per_job_ids:
            if tier < len(ids):
                ordered.append(ids[tier])

    if not ordered:
        _fail_setup(session_id, "No questions could be generated for this session.")
        return

    # Opener is created only after generation succeeded, so a failed build
    # doesn't leave an orphan opener repeating into future existing-question
    # dedup blocks. Served first via position 1.
    if opener_slots:
        opener_id = questions.create_opener_question(job_ids[0], user_id=session["user_id"])
        if opener_id is not None:
            ordered.insert(0, opener_id)

    conn = get_conn()
    try:
        # Clear any slots from a prior failed attempt so a retry rebuilds cleanly.
        conn.execute("DELETE FROM session_answers WHERE session_id = ?", (session_id,))
        for position, qid in enumerate(ordered, start=1):
            conn.execute(
                "INSERT INTO session_answers (session_id, question_id, position) VALUES (?, ?, ?)",
                (session_id, qid, position),
            )
        conn.execute(
            "UPDATE interview_sessions SET setup_status = 'ready', setup_error = NULL WHERE id = ?",
            (session_id,),
        )
        conn.commit()
    except dberr.IntegrityError:
        # The session or its questions were deleted while it was building (e.g. a
        # job was removed mid-generation). Roll back and mark it errored if the
        # session still exists; if it's gone too, _fail_setup is a harmless no-op.
        conn.rollback()
        _fail_setup(session_id, "This interview's job was changed or removed while it was building.")
    finally:
        conn.close()


def reset_setup(session_id: int) -> None:
    """Put a failed session back into 'generating' so build_session can retry."""
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE interview_sessions SET setup_status = 'generating', setup_error = NULL, "
            "busy_since = datetime('now') WHERE id = ? AND setup_status = 'error'",
            (session_id,),
        )
        conn.commit()
    finally:
        conn.close()


def _fail_setup(session_id: int, message: str) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE interview_sessions SET setup_status = 'error', setup_error = ? WHERE id = ?",
            (message, session_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_session(session_id: int, user_id: int):
    conn = get_conn()
    try:
        session = conn.execute(
            """SELECT s.*, j.title AS job_title, j.company AS job_company,
                      j.public_id AS job_pid
               FROM interview_sessions s LEFT JOIN jobs j ON j.id = s.job_id
               WHERE s.id = ? AND s.user_id = ?""",
            (session_id, user_id),
        ).fetchone()
        if session is None:
            return None, [], None
        answers = conn.execute(
            """SELECT a.*, q.text AS question_text, q.type AS question_type,
                      q.skill_display, q.difficulty
               FROM session_answers a JOIN questions q ON q.id = a.question_id
               WHERE a.session_id = ? ORDER BY a.position""",
            (session_id,),
        ).fetchall()
        assessment = conn.execute(
            "SELECT * FROM assessments WHERE session_id = ? ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return session, answers, assessment
    finally:
        conn.close()


def current_answer_row(answers) -> dict | None:
    """The next unanswered question in a session, or None when all are done."""
    for a in answers:
        if a["grade_status"] == "unanswered":
            return a
    return None


def submit_answer(session_id: int, answer_text: str) -> int | None:
    """Record the answer on the current question; returns the answer row id to grade."""
    conn = get_conn()
    try:
        row = conn.execute(
            """SELECT id FROM session_answers
               WHERE session_id = ? AND grade_status = 'unanswered'
               ORDER BY position LIMIT 1""",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            """UPDATE session_answers
               SET answer_text = ?, grade_status = 'grading', answered_at = datetime('now'),
                   busy_since = datetime('now')
               WHERE id = ?""",
            (answer_text, row["id"]),
        )
        conn.commit()
        return row["id"]
    finally:
        conn.close()


def grade_answer(answer_id: int) -> None:
    """Background pipeline for FR-6: grade one answer against its criteria."""
    try:
        done = _grade_answer(answer_id)
    except Exception:
        # Unexpected failure before the grade committed — don't strand 'grading'.
        log.exception("grading answer %s failed unexpectedly", answer_id)
        _mark_terminal_error(
            "UPDATE session_answers SET grade_status = 'error', grade_error = ? "
            "WHERE id = ? AND grade_status = 'grading'",
            (USER_ERROR_GENERIC, answer_id),
        )
        return
    if done is None:
        return
    user_id, drill_id, followup_status = done
    if drill_id is not None and followup_status != "awaiting":
        finish_session(drill_id, assess=False)
    gamification.check_awards(user_id)
    insights.mark_stale(user_id)  # a new score changes the cross-job picture


def _grade_answer(answer_id: int) -> tuple[int, int | None, str] | None:
    """The grading body: read → provider calls (no pooled connection held) →
    write. Returns (user_id, study-drill session id or None, followup_status)
    on success, or None when there was nothing to grade or the LLM failed
    (already terminal-errored)."""
    conn = get_conn()
    try:
        row = conn.execute(
            """SELECT a.id, a.answer_text, q.text AS question_text, q.ideal_answer_criteria,
                      s.user_id
               FROM session_answers a JOIN questions q ON q.id = a.question_id
                    JOIN interview_sessions s ON s.id = a.session_id
               WHERE a.id = ?""",
            (answer_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None or row["answer_text"] is None:
        return None
    try:
        grade: AnswerGrade = get_provider().extract(
            system=ANSWER_GRADING_SYSTEM,
            prompt=answer_grading_prompt(
                row["question_text"], row["ideal_answer_criteria"], row["answer_text"]
            ),
            schema=AnswerGrade,
        )
    except LLMError as exc:
        _mark_terminal_error(
            "UPDATE session_answers SET grade_status = 'error', grade_error = ? WHERE id = ?",
            (str(exc), answer_id),
        )
        return None
    # A weak answer (<= threshold) earns one probing follow-up. Generate it now,
    # in the same background pass, so the graded feedback arrives with the
    # follow-up ready to answer. A generation failure is non-fatal: fall back to
    # no follow-up rather than blocking the graded feedback.
    followup_status, follow_up = "none", None
    if grade.score <= FOLLOWUP_THRESHOLD:
        try:
            follow_up: FollowUpQuestion = get_provider().extract(
                system=FOLLOWUP_SYSTEM,
                prompt=followup_prompt(
                    row["question_text"], row["ideal_answer_criteria"],
                    row["answer_text"], grade.score, grade.feedback,
                ),
                schema=FollowUpQuestion,
            )
            followup_status = "awaiting"
        except LLMError:
            followup_status, follow_up = "none", None
    conn = get_conn()
    try:
        conn.execute(
            """UPDATE session_answers
               SET score = ?, feedback = ?, grade_status = 'ready',
                   followup_status = ?, followup_question = ?, followup_criteria = ?
               WHERE id = ?""",
            (
                grade.score, grade.feedback, followup_status,
                follow_up.question if follow_up else None,
                follow_up.criteria if follow_up else None, answer_id,
            ),
        )
        conn.commit()
        # A study drill is a single question. Grading normally completes the drill
        # (no assessment), but when a follow-up is pending the drill instead
        # completes once that follow-up is answered and graded (see grade_followup).
        drill = conn.execute(
            """SELECT s.id FROM session_answers a JOIN interview_sessions s ON s.id = a.session_id
               WHERE a.id = ? AND s.scope = 'study'""",
            (answer_id,),
        ).fetchone()
    finally:
        conn.close()
    return row["user_id"], (drill["id"] if drill is not None else None), followup_status


def submit_followup(answer_id: int, answer_text: str) -> bool:
    """Record the candidate's answer to the pending follow-up and queue it for
    grading. Returns True if a follow-up was awaiting one (or a prior grade errored
    and is being retried), False otherwise."""
    conn = get_conn()
    try:
        cur = conn.execute(
            """UPDATE session_answers
               SET followup_answer = ?, followup_status = 'grading', followup_error = NULL,
                   busy_since = datetime('now')
               WHERE id = ? AND followup_status IN ('awaiting', 'error')""",
            (answer_text, answer_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def grade_followup(answer_id: int) -> None:
    """Background: grade the follow-up answer against the follow-up's own criteria,
    reusing the answer grader. Completes a pending study drill when done."""
    try:
        done = _grade_followup(answer_id)
    except Exception:
        log.exception("grading follow-up %s failed unexpectedly", answer_id)
        _mark_terminal_error(
            "UPDATE session_answers SET followup_status = 'error', followup_error = ? "
            "WHERE id = ? AND followup_status = 'grading'",
            (USER_ERROR_GENERIC, answer_id),
        )
        return
    if done is None:
        return
    user_id, drill_id = done
    if drill_id is not None:
        finish_session(drill_id, assess=False)
    gamification.check_awards(user_id)
    insights.mark_stale(user_id)  # follow-up score supersedes the original


def _grade_followup(answer_id: int) -> tuple[int, int | None] | None:
    """The follow-up grading body: read → provider call (no pooled connection
    held) → write. Returns (user_id, study-drill session id or None) on
    success, or None when there was nothing to grade or the LLM failed
    (already terminal-errored)."""
    conn = get_conn()
    try:
        row = conn.execute(
            """SELECT a.id, a.followup_question, a.followup_criteria, a.followup_answer,
                      s.user_id
               FROM session_answers a JOIN interview_sessions s ON s.id = a.session_id
               WHERE a.id = ?""",
            (answer_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None or row["followup_answer"] is None or row["followup_question"] is None:
        return None
    try:
        grade: AnswerGrade = get_provider().extract(
            system=ANSWER_GRADING_SYSTEM,
            prompt=answer_grading_prompt(
                row["followup_question"], row["followup_criteria"], row["followup_answer"]
            ),
            schema=AnswerGrade,
        )
    except LLMError as exc:
        _mark_terminal_error(
            "UPDATE session_answers SET followup_status = 'error', followup_error = ? WHERE id = ?",
            (str(exc), answer_id),
        )
        return None
    conn = get_conn()
    try:
        conn.execute(
            """UPDATE session_answers
               SET followup_score = ?, followup_feedback = ?, followup_status = 'ready'
               WHERE id = ?""",
            (grade.score, grade.feedback, answer_id),
        )
        conn.commit()
        drill = conn.execute(
            """SELECT s.id FROM session_answers a JOIN interview_sessions s ON s.id = a.session_id
               WHERE a.id = ? AND s.scope = 'study'""",
            (answer_id,),
        ).fetchone()
    finally:
        conn.close()
    return row["user_id"], (drill["id"] if drill is not None else None)


def retry_followup_grade(answer_id: int) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE session_answers SET followup_status = 'grading', followup_error = NULL, "
            "busy_since = datetime('now') WHERE id = ?",
            (answer_id,),
        )
        conn.commit()
    finally:
        conn.close()


def retry_grade(answer_id: int) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE session_answers SET grade_status = 'grading', grade_error = NULL, "
            "busy_since = datetime('now') WHERE id = ?",
            (answer_id,),
        )
        conn.commit()
    finally:
        conn.close()


def finish_session(session_id: int, *, abandoned: bool = False, assess: bool = True) -> None:
    conn = get_conn()
    try:
        status = "abandoned" if abandoned else "completed"
        # Study drills skip the post-session assessment (and its study-guide
        # refresh) — assess=False keeps assessment_status at 'none'.
        assessment_status = "running" if (assess and not abandoned) else "none"
        conn.execute(
            """UPDATE interview_sessions
               SET status = ?, completed_at = datetime('now'), assessment_status = ?,
                   busy_since = datetime('now')
               WHERE id = ? AND status = 'active'""",
            (status, assessment_status, session_id),
        )
        conn.commit()
    finally:
        conn.close()


def run_assessment(session_id: int) -> None:
    """Background pipeline after completion: assessment (FR-6), then study guide
    refresh (FR-7) and award checks (FR-11)."""
    try:
        done = _run_assessment(session_id)
    except Exception:
        log.exception("assessment for session %s failed unexpectedly", session_id)
        _mark_terminal_error(
            "UPDATE interview_sessions SET assessment_status = 'error', assessment_error = ? "
            "WHERE id = ? AND assessment_status = 'running'",
            (USER_ERROR_GENERIC, session_id),
        )
        return
    if done is None:
        return
    user_id, job_id = done
    gamification.check_awards(user_id)
    if job_id:
        study.generate_guide(job_id)
    else:
        # mixer/global sessions refresh the owner's global guide
        study.generate_global_guide(user_id)


def _run_assessment(session_id: int) -> tuple[int, int | None] | None:
    """The assessment body: read → provider call (no pooled connection held) →
    write. Returns (user_id, job_id) on success, or None when there was
    nothing to assess or the LLM failed (already terminal-errored)."""
    conn = get_conn()
    try:
        session, answers, _ = _session_for_assessment(conn, session_id)
        if session is None:
            return None
        answered = [a for a in answers if a["answer_text"]]
        if not answered:
            conn.execute(
                "UPDATE interview_sessions SET assessment_status = 'none' WHERE id = ?",
                (session_id,),
            )
            conn.commit()
            return None
    finally:
        conn.close()

    if session["scope"] == "mixer":
        n_jobs = len(json.loads(session["mixer_job_ids_json"] or "[]"))
        job_summary = f"Mixed practice across {n_jobs} of the candidate's target jobs"
    elif session["scope"] == "global":
        job_summary = "Global practice across all of the candidate's target jobs"
    elif session["job_title"]:
        job_summary = f"{session['job_title']} at {session['job_company'] or '?'}"
    else:
        job_summary = ""
    qa_block = "\n\n".join(
        f"Q ({a['skill_display']}, {a['difficulty']}): {a['question_text']}\n"
        f"A: {a['answer_text']}\n"
        f"Score: {a['score'] if a['score'] is not None else 'ungraded'}\n"
        f"Feedback: {a['feedback'] or '-'}"
        for a in answered
    )
    try:
        result: SessionAssessment = get_provider().extract(
            system=SESSION_ASSESSMENT_SYSTEM,
            prompt=session_assessment_prompt(job_summary, qa_block),
            schema=SessionAssessment,
        )
    except LLMError as exc:
        _mark_terminal_error(
            "UPDATE interview_sessions SET assessment_status = 'error', assessment_error = ? WHERE id = ?",
            (str(exc), session_id),
        )
        return None

    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO assessments (session_id, summary, per_skill_json, next_actions_json) VALUES (?, ?, ?, ?)",
            (
                session_id,
                result.summary,
                json.dumps([s.model_dump() for s in result.per_skill]),
                json.dumps(result.next_actions),
            ),
        )
        conn.execute(
            "UPDATE interview_sessions SET assessment_status = 'ready' WHERE id = ?",
            (session_id,),
        )
        conn.commit()
    except dberr.IntegrityError:
        conn.rollback()  # session deleted mid-assessment — nothing to store
        return None
    finally:
        conn.close()
    return session["user_id"], session["job_id"]


def _session_for_assessment(conn, session_id: int):
    session = conn.execute(
        """SELECT s.*, j.title AS job_title, j.company AS job_company
           FROM interview_sessions s LEFT JOIN jobs j ON j.id = s.job_id
           WHERE s.id = ?""",
        (session_id,),
    ).fetchone()
    if session is None:
        return None, [], None
    answers = conn.execute(
        """SELECT a.*, q.text AS question_text, q.skill_display, q.difficulty
           FROM session_answers a JOIN questions q ON q.id = a.question_id
           WHERE a.session_id = ? ORDER BY a.position""",
        (session_id,),
    ).fetchall()
    return session, answers, None


def delete_session(session_id: int, user_id: int) -> bool:
    """Remove a session; its answers and assessment cascade (foreign_keys = ON).
    Returns True if a row was deleted."""
    conn = get_conn()
    try:
        cur = conn.execute(
            "DELETE FROM interview_sessions WHERE id = ? AND user_id = ?",
            (session_id, user_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def prune_empty_sessions(user_id: int) -> int:
    """Delete mixer/global sessions that have no questions left. Job-scoped
    sessions cascade away with their job, but mixer/global carry job_id = NULL,
    so when every job they drew from is deleted their answers vanish (questions
    cascade) and the session lingers showing 0/0. Returns the count removed."""
    conn = get_conn()
    try:
        cur = conn.execute(
            """DELETE FROM interview_sessions
               WHERE user_id = ? AND scope IN ('mixer', 'global')
                 AND setup_status != 'generating'
                 AND id NOT IN (SELECT session_id FROM session_answers)""",
            (user_id,),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# Session-history sorting (drill history on /study; same shape as jobs).
# Keys are whitelisted — never interpolate a raw request value.
SESSION_SORT_COLUMNS = frozenset({"topic", "status", "progress", "score", "started"})
SESSION_DEFAULT_SORT = "started"

# Live sessions first, then finished, then walked-away — not alphabetical.
_SESSION_STATUS_ORDER = (
    "CASE s.status WHEN 'active' THEN 0 WHEN 'completed' THEN 1 ELSE 2 END"
)


def _session_order_by(sort: str, dir_sql: str) -> str:
    if sort == "topic":  # empty/null labels last; case-insensitive; stable by recency
        return f"(s.label IS NULL OR s.label = ''), LOWER(s.label) {dir_sql}, s.started_at DESC"
    if sort == "status":
        return f"{_SESSION_STATUS_ORDER} {dir_sql}, s.started_at DESC"
    if sort == "progress":  # answered count (select-list alias)
        return f"answered {dir_sql}, s.started_at DESC"
    if sort == "score":  # ungraded always last
        return f"avg_score IS NULL, avg_score {dir_sql}, s.started_at DESC"
    return f"s.started_at {dir_sql}"  # started / default


def list_sessions(job_id: int | None = None, *, user_id: int,
                  sort: str = SESSION_DEFAULT_SORT, direction: str = "desc"):
    if sort not in SESSION_SORT_COLUMNS:
        sort = SESSION_DEFAULT_SORT
    dir_sql = "ASC" if direction == "asc" else "DESC"
    order = _session_order_by(sort, dir_sql)
    conn = get_conn()
    try:
        where = "WHERE s.user_id = ?"
        params: list = [user_id]
        if job_id is not None:
            where += " AND s.job_id = ?"
            params.append(job_id)
        return conn.execute(
            f"""SELECT s.*, j.title AS job_title, j.company AS job_company,
                       (SELECT COUNT(*) FROM session_answers a
                        WHERE a.session_id = s.id AND a.answer_text IS NOT NULL) AS answered,
                       (SELECT COUNT(*) FROM session_answers a WHERE a.session_id = s.id) AS total,
                       (SELECT ROUND(AVG(COALESCE(a.followup_score, a.score)), 1) FROM session_answers a
                        WHERE a.session_id = s.id AND a.score IS NOT NULL) AS avg_score
                FROM interview_sessions s LEFT JOIN jobs j ON j.id = s.job_id
                {where}
                ORDER BY {order}""",
            params,
        ).fetchall()
    finally:
        conn.close()
