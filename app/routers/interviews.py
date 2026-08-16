import json

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.identity import current_user_id
from app.db import get_conn, public_id_of, resolve_id
from app.services import interviews as interviews_service
from app.services import usage
from app.user_errors import USER_ERROR_QUOTA
from app.web import templates

router = APIRouter(prefix="/interviews")


def owned_session(request: Request, session_pid: str) -> int:
    """Dependency for /{session_pid}/* routes: resolve the session's public id to
    its internal id, confirming ownership; 404s otherwise. Returns the internal id."""
    sid = resolve_id(
        "interview_sessions", session_pid, where="AND user_id = ?", params=(current_user_id(request),)
    )
    if sid is None:
        raise HTTPException(status_code=404)
    return sid


def _session_url(session_id: int, scope: str | None = None) -> str:
    """A session's canonical page URL. Study drills live under the Study section;
    interviews under Interviews."""
    pid = public_id_of("interview_sessions", session_id)
    return f"/app/study/practice/{pid}" if scope == "study" else f"/app/interviews/{pid}"


def render_session_page(request: Request, session, answers, assessment, active_nav: str):
    """Render the shared session page. Used by /interviews/{pid} (interviews) and
    /study/practice/{pid} (study drills) — same engine, different section chrome."""
    return templates.TemplateResponse(
        request,
        "interview_session.html",
        {
            "active_nav": active_nav,
            "session": session,
            "answers": answers,
            "assessment": assessment,
            "current": interviews_service.current_answer_row(answers),
            "answered_count": sum(1 for a in answers if a["answer_text"]),
        },
    )


@router.get("")
def interviews_page(request: Request):
    uid = current_user_id(request)
    all_sessions = interviews_service.list_sessions(user_id=uid)
    return templates.TemplateResponse(
        request,
        "interviews.html",
        {
            "active_nav": "interviews",
            "sessions": [s for s in all_sessions if s["scope"] != "study"],
            "ready_jobs": interviews_service.interviewable_jobs(user_id=uid),
            "start_error": request.query_params.get("err"),
        },
    )


@router.post("")
def start_session(
    request: Request,
    background: BackgroundTasks,
    scope: str = Form("job"),
    job_id: str | None = Form(None),
    job_ids: list[str] = Form([]),
    count: int = Form(10),
    skip_opener: str | None = Form(None),
):
    """Start a session in one of two scopes — job (one job) or mixer (several).
    The form posts jobs by their public id; resolve to internal ids (scoped to
    the user), then build the session's questions in the background while the
    page polls until ready."""
    uid = current_user_id(request)
    # check() now / record() only once a session actually exists — a start that
    # can't create a session (stale form, deleted jobs) must not be charged.
    usage.check(uid, "questions")

    def to_internal(pids):
        out = []
        for p in pids:
            iid = resolve_id("jobs", p, where="AND user_id = ?", params=(uid,))
            if iid is not None:
                out.append(iid)
        return out

    if scope == "mixer":
        selected = to_internal(job_ids if len(job_ids) > 1 else ([job_id] if job_id else job_ids))
        session_id = interviews_service.create_session(
            "mixer" if len(selected) > 1 else "job", selected, count, user_id=uid
        )
    elif scope == "global":
        session_id = interviews_service.create_session("global", [], count, user_id=uid)
    else:
        # Job-focused sessions open with the "tell me about yourself" opener
        # unless the form opted out (one-click starts default to including it).
        session_id = interviews_service.create_session(
            "job", to_internal([job_id] if job_id else []), count, user_id=uid,
            include_opener=not skip_opener,
        )
    if session_id is None:
        # Every selected job was invalid or no longer interviewable — say so
        # instead of silently bouncing back (and charge nothing).
        return RedirectResponse("/app/interviews?err=nojobs", status_code=303)
    usage.record(uid, "questions")
    # count/opener were stored at creation; the build reads them from the row.
    background.add_task(interviews_service.build_session, session_id)
    return RedirectResponse(_session_url(session_id), status_code=303)


@router.get("/{session_pid}")
def session_page(request: Request, session_pid: str):
    uid = current_user_id(request)
    session_id = resolve_id("interview_sessions", session_pid, where="AND user_id = ?", params=(uid,))
    if session_id is None:  # missing or not owned — back to the list
        return RedirectResponse("/app/interviews", status_code=303)
    session, answers, assessment = interviews_service.get_session(session_id, uid)
    if session is None:  # deleted between resolve and read (TOCTOU) — back to list
        return RedirectResponse("/app/interviews", status_code=303)
    if session["scope"] == "study":  # drills live in the Study section
        return RedirectResponse(f"/app/study/practice/{session_pid}", status_code=303)
    return render_session_page(request, session, answers, assessment, "interviews")


@router.get("/{session_pid}/setup")
def session_setup(request: Request, session_id: int = Depends(owned_session)):
    """Polled while a session's questions build in the background. Redirects into
    the interview once ready; otherwise returns the spinner/error partial."""
    session, _, _ = interviews_service.get_session(session_id, current_user_id(request))
    if session is None:
        return HTMLResponse("", headers={"HX-Redirect": "/app/interviews"})
    if session["setup_status"] == "ready":
        return HTMLResponse("", headers={"HX-Redirect": _session_url(session_id, session["scope"])})
    return templates.TemplateResponse(request, "partials/session_setup.html", {"session": session})


@router.post("/{session_pid}/setup/retry")
def retry_setup(
    request: Request, background: BackgroundTasks, session_id: int = Depends(owned_session)
):
    uid = current_user_id(request)
    session, _, _ = interviews_service.get_session(session_id, uid)
    if session is None:
        return HTMLResponse("", headers={"HX-Redirect": "/app/interviews"})
    if session["scope"] == "study":
        # A drill's topic parameters live only in its original build call —
        # build_session can't rebuild one, so a drill retry would charge for a
        # guaranteed failure. The error partial offers a fresh drill instead.
        return templates.TemplateResponse(request, "partials/session_setup.html", {"session": session})
    usage.check(uid, "questions")
    # Only the request that wins the error → generating claim enqueues a build
    # (and gets charged) — a double-click or an already-recovered session must
    # not start a second concurrent build. The rebuild reuses the count and
    # opener choice stored on the session row.
    if interviews_service.reset_setup(session_id):
        usage.record(uid, "questions")
        background.add_task(interviews_service.build_session, session_id)
        session, _, _ = interviews_service.get_session(session_id, uid)
        if session is None:
            return HTMLResponse("", headers={"HX-Redirect": "/app/interviews"})
    return templates.TemplateResponse(request, "partials/session_setup.html", {"session": session})


@router.post("/{session_pid}/answer")
def submit_answer(
    request: Request, background: BackgroundTasks, answer_text: str = Form(...),
    session_id: int = Depends(owned_session),
):
    answer_id = interviews_service.submit_answer(session_id, answer_text.strip())
    if answer_id is None:
        return HTMLResponse("", headers={"HX-Redirect": _session_url(session_id)})
    # The typed answer is already saved; a refused grade degrades to the error
    # state (with its retry button) instead of losing the answer to a 429.
    if usage.try_spend(current_user_id(request), "grade"):
        background.add_task(interviews_service.grade_answer, answer_id)
    else:
        interviews_service.fail_grade(answer_id, USER_ERROR_QUOTA)
    return _feedback_partial(request, session_id, answer_id)


@router.get("/{session_pid}/feedback/{answer_pid}")
def answer_feedback(request: Request, answer_pid: str, session_id: int = Depends(owned_session)):
    answer_id = resolve_id("session_answers", answer_pid, where="AND session_id = ?", params=(session_id,))
    if answer_id is None:
        return HTMLResponse("")
    return _feedback_partial(request, session_id, answer_id)


def _feedback_partial(request: Request, session_id: int, answer_id: int):
    conn = get_conn()
    try:
        # Scope the answer to its session (the caller verified the session is owned).
        answer = conn.execute(
            """SELECT a.*, q.text AS question_text, q.type AS question_type,
                      q.skill_display, q.difficulty
               FROM session_answers a JOIN questions q ON q.id = a.question_id
               WHERE a.id = ? AND a.session_id = ?""",
            (answer_id, session_id),
        ).fetchone()
        remaining = conn.execute(
            "SELECT COUNT(*) FROM session_answers WHERE session_id = ? AND grade_status = 'unanswered'",
            (session_id,),
        ).fetchone()[0]
        total = conn.execute(
            "SELECT COUNT(*) FROM session_answers WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]
        scope = conn.execute(
            "SELECT scope FROM interview_sessions WHERE id = ?", (session_id,)
        ).fetchone()["scope"]
    finally:
        conn.close()
    if answer is None:
        return HTMLResponse("")
    return templates.TemplateResponse(
        request,
        "partials/answer_feedback.html",
        {
            "answer": answer,
            "session_pid": public_id_of("interview_sessions", session_id),
            "remaining": remaining,
            "total": total,
            "answered_count": total - remaining,
            "is_drill": scope == "study",
        },
    )


@router.post("/{session_pid}/feedback/{answer_pid}/regrade")
def regrade(
    request: Request, answer_pid: str, background: BackgroundTasks,
    session_id: int = Depends(owned_session),
):
    answer_id = resolve_id("session_answers", answer_pid, where="AND session_id = ?", params=(session_id,))
    if answer_id is None:
        return HTMLResponse("")
    usage.spend(current_user_id(request), "grade")
    interviews_service.retry_grade(answer_id)
    background.add_task(interviews_service.grade_answer, answer_id)
    return _feedback_partial(request, session_id, answer_id)


@router.post("/{session_pid}/feedback/{answer_pid}/followup")
def submit_followup_answer(
    request: Request, answer_pid: str, background: BackgroundTasks,
    followup_text: str = Form(...), session_id: int = Depends(owned_session),
):
    """Record the candidate's answer to the interviewer's follow-up, then grade it."""
    answer_id = resolve_id("session_answers", answer_pid, where="AND session_id = ?", params=(session_id,))
    if answer_id is None:
        return HTMLResponse("")
    if interviews_service.submit_followup(answer_id, followup_text.strip()):
        # Mirror submit_answer: keep the saved follow-up answer, degrade the grade.
        if usage.try_spend(current_user_id(request), "grade_followup"):
            background.add_task(interviews_service.grade_followup, answer_id)
        else:
            interviews_service.fail_followup_grade(answer_id, USER_ERROR_QUOTA)
    return _feedback_partial(request, session_id, answer_id)


@router.post("/{session_pid}/feedback/{answer_pid}/followup/regrade")
def regrade_followup(
    request: Request, answer_pid: str, background: BackgroundTasks,
    session_id: int = Depends(owned_session),
):
    answer_id = resolve_id("session_answers", answer_pid, where="AND session_id = ?", params=(session_id,))
    if answer_id is None:
        return HTMLResponse("")
    usage.spend(current_user_id(request), "grade_followup")
    interviews_service.retry_followup_grade(answer_id)
    background.add_task(interviews_service.grade_followup, answer_id)
    return _feedback_partial(request, session_id, answer_id)


@router.post("/{session_pid}/next")
def next_question(
    request: Request, background: BackgroundTasks, session_id: int = Depends(owned_session)
):
    session, answers, _ = interviews_service.get_session(session_id, current_user_id(request))
    if session is None:
        return HTMLResponse("", headers={"HX-Redirect": "/app/interviews"})
    current = interviews_service.current_answer_row(answers)
    if current is None:
        is_drill = session["scope"] == "study"
        interviews_service.finish_session(session_id, assess=not is_drill)
        if not is_drill:  # study drills get no post-session assessment
            # Finishing must never be blocked; a refused assessment degrades
            # to the error state and its retry button.
            if usage.try_spend(current_user_id(request), "assessment"):
                background.add_task(interviews_service.run_assessment, session_id)
            else:
                interviews_service.fail_assessment(session_id, USER_ERROR_QUOTA)
        return Response(headers={"HX-Redirect": _session_url(session_id, session["scope"])})
    return templates.TemplateResponse(
        request,
        "partials/question_card.html",
        {
            "session": session,
            "current": current,
            "answered_count": sum(1 for a in answers if a["answer_text"]),
            "total": len(answers),
            "oob": True,  # partial swap: re-emit the page-header count badge
        },
    )


@router.post("/{session_pid}/finish")
def finish(request: Request, background: BackgroundTasks, session_id: int = Depends(owned_session)):
    uid = current_user_id(request)
    session, answers, _ = interviews_service.get_session(session_id, uid)
    if session is None:
        return RedirectResponse("/app/interviews", status_code=303)
    answered = any(a["answer_text"] for a in answers)
    if not answered:
        # No answers recorded — drop the session rather than keep an empty
        # abandoned record cluttering history (esp. mixer/global sessions,
        # which don't cascade away with a job).
        interviews_service.delete_session(session_id, uid)
        return RedirectResponse("/app/interviews", status_code=303)
    is_drill = session["scope"] == "study"
    interviews_service.finish_session(session_id, assess=not is_drill)
    if not is_drill:  # study drills get no post-session assessment
        # See next_question: session completion survives a refused assessment.
        if usage.try_spend(uid, "assessment"):
            background.add_task(interviews_service.run_assessment, session_id)
        else:
            interviews_service.fail_assessment(session_id, USER_ERROR_QUOTA)
    return RedirectResponse(_session_url(session_id, session["scope"]), status_code=303)


@router.post("/{session_pid}/delete")
def delete_session(request: Request, session_id: int = Depends(owned_session)):
    uid = current_user_id(request)
    session, _, _ = interviews_service.get_session(session_id, uid)
    scope = session["scope"] if session else None
    interviews_service.delete_session(session_id, uid)
    if "hx-request" not in request.headers:
        # Plain form (e.g. the drill result page) — send the user back to the
        # right list: study drills live under /study, other sessions /interviews.
        return RedirectResponse("/app/study" if scope == "study" else "/app/interviews", status_code=303)
    return HTMLResponse(
        "",
        headers={"HX-Trigger": json.dumps({"toast": {"message": "Session removed.", "tone": "success"}})},
    )


@router.get("/{session_pid}/assessment")
def assessment_partial(request: Request, session_id: int = Depends(owned_session)):
    session, _, assessment = interviews_service.get_session(session_id, current_user_id(request))
    if session is None:
        return HTMLResponse("")
    return templates.TemplateResponse(
        request,
        "partials/assessment.html",
        {"session": session, "assessment": assessment},
    )


@router.post("/{session_pid}/assessment/retry")
def retry_assessment(
    request: Request, background: BackgroundTasks, session_id: int = Depends(owned_session)
):
    usage.spend(current_user_id(request), "assessment")
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE interview_sessions SET assessment_status = 'running', assessment_error = NULL, "
            "busy_since = datetime('now') WHERE id = ? AND user_id = ?",
            (session_id, current_user_id(request)),
        )
        conn.commit()
    finally:
        conn.close()
    background.add_task(interviews_service.run_assessment, session_id)
    return assessment_partial(request, session_id=session_id)
