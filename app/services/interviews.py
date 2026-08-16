import json
import logging
import random

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

# The one place session size is bounded. The HTTP form offers 5/10, but the
# service enforces the range so no caller (or future form) can slip past it.
MIN_QUESTIONS, MAX_QUESTIONS = 3, 10


def clamp_count(count: int) -> int:
    return min(max(count, MIN_QUESTIONS), MAX_QUESTIONS)


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
    """Rolling average score and answer count per canonical skill (the mastery
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
    scope: str, job_ids: list[int], count: int = 10, *, user_id: int,
    include_opener: bool = False,
) -> int | None:
    """Create an interview session over one or more jobs. Questions aren't built
    here — the session starts in setup_status 'generating' and build_session()
    fills it in the background. The requested count and opener choice are stored
    on the row so a setup retry rebuilds the same session the user asked for.
    Returns the session id, or None if no selected job is interviewable."""
    conn = get_conn()
    try:
        interviewable = {r["id"] for r in interviewable_jobs(conn, user_id=user_id)}
        if scope == "global":
            job_ids = list(interviewable)
        job_ids = [jid for jid in job_ids if jid in interviewable]
        if not job_ids:
            return None
        cur = conn.execute(
            """INSERT INTO interview_sessions
               (user_id, scope, job_id, mixer_job_ids_json, question_count,
                include_opener, setup_status, busy_since)
               VALUES (?, ?, ?, ?, ?, ?, 'generating', datetime('now')) RETURNING id""",
            (
                user_id,
                scope,
                job_ids[0] if scope == "job" else None,
                json.dumps(job_ids) if scope != "job" else None,
                clamp_count(count),
                1 if (include_opener and scope == "job") else 0,
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
            "SELECT user_id, setup_status, setup_run FROM interview_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()
    if owner is None:  # session deleted before the build started
        return
    if owner["setup_status"] != "generating":  # no claim, no LLM work
        return
    run = owner["setup_run"]
    try:
        qid = questions.generate_for_topic(
            job_id, topic, why_it_matters, how_it_will_be_tested, user_id=owner["user_id"]
        )
    except LLMError as exc:
        _fail_setup(session_id, str(exc), run=run)
        return
    except Exception:
        log.exception("study drill %s setup failed unexpectedly", session_id)
        _fail_setup(session_id, USER_ERROR_GENERIC, run=run)
        return
    if qid is None:
        _fail_setup(session_id, "Couldn't generate a practice question for this topic.", run=run)
        return

    conn = get_conn()
    try:
        conn.execute("DELETE FROM session_answers WHERE session_id = ?", (session_id,))
        conn.execute(
            "INSERT INTO session_answers (session_id, question_id, position) VALUES (?, ?, 1)",
            (session_id, qid),
        )
        if not _finish_setup(conn, session_id, run):
            conn.rollback()  # superseded — discard this build's work, question included
            _delete_questions([qid])
            return
        conn.commit()
    except dberr.IntegrityError:
        conn.rollback()
        _abort_setup(
            session_id, run,
            "This drill's job was changed or removed while it was building.", [qid],
        )
    except Exception:
        conn.rollback()
        log.exception("study drill %s setup failed unexpectedly", session_id)
        _abort_setup(session_id, run, USER_ERROR_GENERIC, [qid])
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


def build_session(session_id: int) -> None:
    """Background: generate this session's questions (per job, tailored by the
    profile) and lay them out as answerable slots. Sets setup_status ready/error
    for the polling UI. Round-robins across jobs so multi-job sessions stay varied.
    The session's stored question_count and include_opener decide its shape —
    creation and retry both build exactly what the user asked for. The opener
    (job-scope only) replaces one generated question, so count stays the total."""
    try:
        _build_session(session_id)
    except Exception:  # never strand setup_status at 'generating' (A10)
        # Backstop for failures before the claim was read — past that point
        # _build_session handles its own errors with a setup_run fence. This
        # write can only be status-fenced (the run is unknown here).
        log.exception("session %s setup failed unexpectedly", session_id)
        _mark_terminal_error(
            "UPDATE interview_sessions SET setup_status = 'error', setup_error = ? "
            "WHERE id = ? AND setup_status = 'generating'",
            (USER_ERROR_GENERIC, session_id),
        )


def _build_session(session_id: int) -> None:
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

    # Only the build that holds the 'generating' claim may do LLM work; the
    # setup_run captured here fences every terminal write below, so a build
    # superseded mid-flight (reaped to error, then retried) discards its work
    # instead of overwriting the newer run's state.
    if session["setup_status"] != "generating":
        return
    run = session["setup_run"]
    count = clamp_count(session["question_count"] or MAX_QUESTIONS)  # pre-migration rows: 10
    include_opener = bool(session["include_opener"])

    # The stored job list can go stale between creation and (re)build — jobs
    # deleted or re-extracting. Draw only from jobs that are still
    # interviewable so slots aren't wasted on dead ids.
    live = {r["id"] for r in interviewable_jobs(user_id=session["user_id"])}
    job_ids = [jid for jid in job_ids if jid in live]
    if not job_ids:
        _fail_setup(session_id, "This session has no jobs to draw questions from.", run=run)
        return

    # Track every question row this build creates so any failure or discard
    # path can remove them — stranded rows would otherwise pollute the
    # avoid-repeats block fed to future generations (and duplicate openers).
    created: list[int] = []
    try:
        # The opener replaces one generated question (count stays the total).
        # It is deterministic (no LLM), so create it first: if it can't be
        # created (job re-extracting, say), fall back to generating the full
        # count instead of silently shipping a short session.
        opener_id = None
        if include_opener and session["scope"] == "job":
            opener_id = questions.create_opener_question(job_ids[0], user_id=session["user_id"])
            if opener_id is not None:
                created.append(opener_id)
        slots = count - (1 if opener_id is not None else 0)

        # `count` is the contract: with more jobs than slots, sample which jobs
        # this session draws from rather than silently exceeding the requested size.
        if len(job_ids) > slots:
            job_ids = random.sample(job_ids, slots)

        per_job_ids: list[list[int]] = []
        for jid, n in zip(job_ids, _distribute(slots, len(job_ids))):
            ids = questions.generate_for_session(jid, n, user_id=session["user_id"])
            created.extend(ids)
            per_job_ids.append(ids)
    except LLMError as exc:
        _abort_setup(session_id, run, str(exc), created)
        return
    except Exception:
        log.exception("session %s setup failed unexpectedly", session_id)
        _abort_setup(session_id, run, USER_ERROR_GENERIC, created)
        return

    # Round-robin merge so consecutive questions come from different jobs.
    ordered: list[int] = []
    for tier in range(max((len(x) for x in per_job_ids), default=0)):
        for ids in per_job_ids:
            if tier < len(ids):
                ordered.append(ids[tier])

    if not ordered:
        _abort_setup(session_id, run, "No questions could be generated for this session.", created)
        return

    if opener_id is not None:  # served first via position 1
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
        if not _finish_setup(conn, session_id, run):
            # A retry superseded this build — discard everything it produced.
            conn.rollback()
            _delete_questions(created)
            return
        conn.commit()
    except dberr.IntegrityError:
        # The session or its questions were deleted while it was building (e.g. a
        # job was removed mid-generation). Roll back and mark it errored if the
        # session still exists; if it's gone too, the abort is a harmless no-op
        # (cascaded questions are already gone).
        conn.rollback()
        _abort_setup(
            session_id, run,
            "This interview's job was changed or removed while it was building.",
            created,
        )
    except Exception:
        conn.rollback()
        log.exception("session %s setup failed unexpectedly", session_id)
        _abort_setup(session_id, run, USER_ERROR_GENERIC, created)
    finally:
        conn.close()


def _finish_setup(conn, session_id: int, run: int) -> bool:
    """The one terminal ready-write for session builds, fenced by setup_run
    alone: a build that was merely reaped (no retry claimed it — run unchanged)
    still lands its finished work, per the reaper contract; a build superseded
    by a retry (run bumped) does not. Runs inside the caller's transaction;
    returns whether the write landed."""
    cur = conn.execute(
        "UPDATE interview_sessions SET setup_status = 'ready', setup_error = NULL "
        "WHERE id = ? AND setup_run = ?",
        (session_id, run),
    )
    return cur.rowcount > 0


def _abort_setup(session_id: int, run: int, message: str, created: list[int]) -> None:
    """Fail a build: remove every question it created (no orphans feeding the
    avoid-repeats block, no duplicate openers), then error the session — fenced
    by setup_run so a superseded build can't clobber a newer run's state."""
    _delete_questions(created)
    _fail_setup(session_id, message, run=run)


def _delete_questions(question_ids: list[int]) -> None:
    """Remove questions created for a build that then failed or was discarded,
    so retries don't accumulate duplicates in the job's question history."""
    if not question_ids:
        return
    conn = get_conn()
    try:
        marks = ",".join("?" for _ in question_ids)
        conn.execute(f"DELETE FROM questions WHERE id IN ({marks})", tuple(question_ids))
        conn.commit()
    finally:
        conn.close()


def reset_setup(session_id: int) -> bool:
    """Claim a failed session back into 'generating' so build_session can retry.
    Bumps setup_run so any still-running older build is fenced out. Returns True
    only when this call won the claim — a lost claim (double-click, already
    rebuilt) must enqueue nothing and charge nothing."""
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE interview_sessions SET setup_status = 'generating', setup_error = NULL, "
            "busy_since = datetime('now'), setup_run = setup_run + 1 "
            "WHERE id = ? AND setup_status = 'error'",
            (session_id,),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def _fail_setup(session_id: int, message: str, run: int | None = None) -> None:
    conn = get_conn()
    try:
        sql = "UPDATE interview_sessions SET setup_status = 'error', setup_error = ? WHERE id = ?"
        params: tuple = (message, session_id)
        if run is not None:  # a fenced build must not clobber a newer run's state
            sql += " AND setup_run = ?"
            params += (run,)
        conn.execute(sql, params)
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
    """Record the answer on the current question; returns the answer row id to
    grade, or None when there is nothing to answer — including a session that
    already finished (a stale tab or replayed POST must not add answers to a
    completed session whose assessment has run)."""
    conn = get_conn()
    try:
        row = conn.execute(
            """SELECT a.id FROM session_answers a
               JOIN interview_sessions s ON s.id = a.session_id
               WHERE a.session_id = ? AND a.grade_status = 'unanswered'
                 AND s.status = 'active'
               ORDER BY a.position LIMIT 1""",
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
    """Background pipeline: grade one answer against its criteria."""
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
    """Background pipeline after completion: assessment, then a study-guide
    refresh and award checks."""
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
