import json

from fastapi import APIRouter, BackgroundTasks, Form, Request
from fastapi.responses import RedirectResponse

from app.identity import current_user_id
from app.db import get_state, public_id_of, resolve_id, set_state
from app.routers.interviews import render_session_page
from app.services import interviews as interviews_service
from app.services import jobs as jobs_service
from app.services import usage
from app.services import study as study_service
from app.web import templates

router = APIRouter(prefix="/study")


def _global_context(uid: int) -> dict:
    return {
        "guide": study_service.latest_global_guide(uid),
        "status": get_state(study_service.global_status_key(uid)) or "none",
        "error": get_state(study_service.global_error_key(uid)),
        "topic_stats": study_service.topic_drill_stats(None, uid),
    }


@router.get("")
def study_page(request: Request, background: BackgroundTasks, job: str | None = None,
               view: str = "guides",
               sort: str = interviews_service.SESSION_DEFAULT_SORT, dir: str = "desc"):
    """The study hub, split into two top-level views: `guides` (the global focus
    plan or a chosen job's study guide) and `drills` (a history of the impromptu
    drills you've run). `job` (a job public_id) selects a per-job guide; landing
    on a job that has no guide yet (e.g. via a "not generated" switcher pill)
    kicks generation off on the fly."""
    uid = current_user_id(request)
    view = "drills" if view == "drills" else "guides"
    if sort not in interviews_service.SESSION_SORT_COLUMNS:
        sort = interviews_service.SESSION_DEFAULT_SORT
    dir = "asc" if dir == "asc" else "desc"
    ctx = {
        "active_nav": "study",
        "view": view,
        "sort": sort,
        "dir": dir,
        "switcher_jobs": study_service.switcher_jobs(uid),
        "selected_job": None,
        "drills": [s for s in interviews_service.list_sessions(user_id=uid, sort=sort, direction=dir)
                   if s["scope"] == "study"],
    }
    if view == "guides":
        job_id = resolve_id("jobs", job, where="AND user_id = ?", params=(uid,)) if job else None
        if job_id is not None:
            job_row, _requirements, _fit = jobs_service.get_job(job_id, uid)
            ctx["selected_job"] = job_row
            ctx["job"] = job_row  # tab_study.html expects `job`
            ctx["guide"] = study_service.latest_guide(job_id)
            ctx["topic_stats"] = study_service.topic_drill_stats(job_id, uid)
            # No guide yet → generate it now and render the polling state; the
            # partial swaps in the finished guide once ready. Idempotent: skip
            # when one is already running, errored (show retry), or already built.
            # try_spend: a GET must never 429 — over quota, the page just shows
            # the Generate button (whose POST surfaces the refusal).
            if (ctx["guide"] is None and job_row["study_status"] not in ("running", "error")
                    and usage.try_spend(uid, "study_guide")
                    and study_service.mark_guide_generating(job_id, uid)):
                background.add_task(study_service.generate_guide, job_id)
                job_row, _r, _f = jobs_service.get_job(job_id, uid)
                ctx["selected_job"] = ctx["job"] = job_row
        else:
            ctx.update(_global_context(uid))
    return templates.TemplateResponse(request, "study.html", ctx)


@router.get("/content")
def study_content(request: Request):
    return templates.TemplateResponse(
        request, "partials/global_guide.html",
        {"active_nav": "study", **_global_context(current_user_id(request))},
    )


@router.post("/generate")
def generate(request: Request, background: BackgroundTasks):
    # Skip if a run is already in flight so a double-submit can't enqueue a
    # second (billed) global-guide generation (A06 cost/duplication).
    uid = current_user_id(request)
    if (get_state(study_service.global_status_key(uid)) or "none") != "running":
        usage.spend(uid, "study_guide_global")
        set_state(study_service.global_status_key(uid), "running")
        set_state(study_service.global_error_key(uid), None)
        background.add_task(study_service.generate_global_guide, uid)
    return templates.TemplateResponse(
        request, "partials/global_guide.html", {"active_nav": "study", **_global_context(uid)}
    )


@router.post("/drill")
def global_drill(request: Request, background: BackgroundTasks, topic_index: int = Form(...)):
    """Launch a job-less drill on a Global-focus-plan topic: general practice from
    the topic + the candidate's profile, not tied to a specific role."""
    uid = current_user_id(request)
    guide = study_service.latest_global_guide(uid)
    if guide is None:
        return RedirectResponse("/app/study", status_code=303)
    topics = json.loads(guide["content_json"]).get("topics", [])
    if not 0 <= topic_index < len(topics):
        return RedirectResponse("/app/study", status_code=303)
    t = topics[topic_index]
    label = t.get("topic") or "Study drill"
    usage.spend(uid, "drill")
    session_id = interviews_service.create_study_drill(None, label, user_id=uid)
    if session_id is None:
        return RedirectResponse("/app/study", status_code=303)
    background.add_task(
        interviews_service.build_study_drill, session_id, None, label,
        t.get("why_it_matters", ""), t.get("how_it_will_be_tested", ""),
    )
    return RedirectResponse(
        f"/app/study/practice/{public_id_of('interview_sessions', session_id)}", status_code=303
    )


@router.get("/practice/{session_pid}")
def practice(request: Request, session_pid: str):
    """The live drill (and its finished transcript) — the interview session engine
    rendered inside the Study section. Non-study sessions bounce to Interviews."""
    uid = current_user_id(request)
    session_id = resolve_id("interview_sessions", session_pid, where="AND user_id = ?", params=(uid,))
    if session_id is None:
        return RedirectResponse("/app/study", status_code=303)
    session, answers, assessment = interviews_service.get_session(session_id, uid)
    if session is None:  # deleted between resolve and read (TOCTOU)
        return RedirectResponse("/app/study", status_code=303)
    if session["scope"] != "study":
        return RedirectResponse(f"/app/interviews/{session_pid}", status_code=303)
    return render_session_page(request, session, answers, assessment, "study")


@router.post("/practice/{session_pid}/again")
def practice_again(request: Request, background: BackgroundTasks, session_pid: str):
    """'Practice another': spin up a fresh single-question drill on the same topic
    as an existing drill, and drop the user into it."""
    uid = current_user_id(request)
    session_id = resolve_id("interview_sessions", session_pid, where="AND user_id = ?", params=(uid,))
    if session_id is None:
        return RedirectResponse("/app/study", status_code=303)
    session, _, _ = interviews_service.get_session(session_id, uid)
    if session is None or session["scope"] != "study":
        return RedirectResponse("/app/study", status_code=303)
    job_id = session["job_id"]  # None for a Global-focus-plan drill
    topic = session["label"] or "Study drill"
    usage.spend(uid, "drill")
    new_id = interviews_service.create_study_drill(job_id, topic, user_id=uid)
    if new_id is None:
        return RedirectResponse(f"/app/study/practice/{session_pid}", status_code=303)
    why, how = study_service.topic_detail(job_id, topic, user_id=uid)
    background.add_task(
        interviews_service.build_study_drill, new_id, job_id, topic, why, how
    )
    return RedirectResponse(
        f"/app/study/practice/{public_id_of('interview_sessions', new_id)}", status_code=303
    )
