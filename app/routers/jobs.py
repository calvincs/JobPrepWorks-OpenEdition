import json
import re

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from app.identity import current_user_id
from app.config import MAX_UPLOAD_BYTES
from app.db import public_id_of, resolve_id
from app.services import analysis as analysis_service
from app.services import documents as documents_service
from app.services import interviews as interviews_service
from app.services import jobs as jobs_service
from app.services import pitch as pitch_service
from app.services import profile as profile_service
from app.services import pulse as pulse_service
from app.services import usage
from app.services import resume as resume_service
from app.services import study as study_service
from app.services import tracking as tracking_service
from app.services import users as users_service
from app.web import templates

router = APIRouter(prefix="/jobs")

JOB_STATUSES = ["researching", "training", "applied", "interviewing", "offer", "rejected", "withdrawn"]


def owned_job(request: Request, job_pid: str) -> int:
    """Dependency for /{job_pid}/* routes: resolve the job's opaque public id to
    its internal integer id, confirming it's owned by the acting user. 404s
    otherwise. Handlers receive the internal id and use it as before."""
    jid = resolve_id("jobs", job_pid, where="AND user_id = ?", params=(current_user_id(request),))
    if jid is None:
        raise HTTPException(status_code=404)
    return jid


@router.get("")
def jobs_page(
    request: Request,
    error: str | None = None,
    sort: str = jobs_service.DEFAULT_SORT,
    dir: str = "desc",
):
    uid = current_user_id(request)
    if sort not in jobs_service.SORT_COLUMNS:
        sort = jobs_service.DEFAULT_SORT
    dir = "asc" if dir == "asc" else "desc"
    jobs = jobs_service.list_jobs(sort, dir, user_id=uid)
    return templates.TemplateResponse(
        request,
        "jobs.html",
        {
            "active_nav": "jobs",
            "jobs": jobs,
            "error": error,
            "sort": sort,
            "dir": dir,
            "has_profile": profile_service.has_profile_facts(uid),
        },
    )


@router.post("")
def create_job(
    request: Request,
    background: BackgroundTasks,
    posting_text: str = Form(""),
    url: str = Form(""),
    posting_file: UploadFile | None = None,
):
    """Add a job: free-form paste or file upload; URL is optional metadata only.
    Deliberately sync (`def`): the multipart body is already spooled before the
    handler runs, and the PDF/DOCX parse + DB writes below are blocking — as
    `async def` they'd stall the event loop for every user."""
    uid = current_user_id(request)
    raw_text = posting_text.strip()
    source = "pasted"
    source_document_id = None

    if posting_file is not None and posting_file.filename:
        content = posting_file.file.read(MAX_UPLOAD_BYTES + 1)  # bounded read (A06)
        try:
            source_document_id = documents_service.save_upload(
                posting_file.filename, content, purpose="job", user_id=uid
            )
            raw_text = documents_service.parse_document(source_document_id)
            source = "file"
        except (documents_service.UnsupportedFileType, documents_service.FileTooLarge) as exc:
            return templates.TemplateResponse(
                request,
                "jobs.html",
                {"active_nav": "jobs", "jobs": jobs_service.list_jobs(user_id=uid), "error": str(exc)},
                status_code=422,
            )
        except ValueError:
            # parse_document already recorded curated copy on the document row;
            # keep the upload boundary message internal-free.
            return templates.TemplateResponse(
                request,
                "jobs.html",
                {"active_nav": "jobs", "jobs": jobs_service.list_jobs(user_id=uid),
                 "error": "Couldn't read that file — try a different PDF, DOCX, or text file."},
                status_code=422,
            )

    if not raw_text:
        return templates.TemplateResponse(
            request,
            "jobs.html",
            {
                "active_nav": "jobs",
                "jobs": jobs_service.list_jobs(user_id=uid),
                "error": "Paste the job posting text or upload a file.",
            },
            status_code=422,
        )

    from app.text import normalize_posting, safe_external_url

    # Ledger before the row exists: a refused intake must leave nothing behind.
    usage.spend(uid, "intake")
    job_id = jobs_service.create_job(
        normalize_posting(raw_text), user_id=uid, source=source,
        source_document_id=source_document_id, url=safe_external_url(url),
    )
    background.add_task(jobs_service.run_intake, job_id)
    return RedirectResponse(f"/app/jobs/{public_id_of('jobs', job_id)}", status_code=303)


JOB_TABS = ("overview", "fit", "pitch", "resume", "pulse", "study", "sessions", "activity")


def _tab_context(request: Request, job_id: int, tab: str) -> dict | None:
    uid = current_user_id(request)
    job, requirements, fit = jobs_service.get_job(job_id, uid)
    if job is None:  # missing or not owned — callers treat as 404/redirect
        return None
    ctx = {"job": job, "requirements": requirements, "fit": fit, "statuses": JOB_STATUSES}
    if tab == "fit":
        ctx["fit_history"] = jobs_service.fit_history(job_id, uid)
        ctx["has_profile"] = profile_service.has_profile_facts(uid)
        ctx["has_direction"] = profile_service.has_direction_facts(uid)
        ctx["resolved_gaps"] = (
            analysis_service.resolved_gap_keys(fit["id"], user_id=uid) if fit else set()
        )
    elif tab == "pitch":
        ctx["pitch"] = pitch_service.latest_pitch(job_id)
        ctx["has_profile"] = profile_service.has_profile_facts(uid)
    elif tab == "resume":
        ctx["resume"] = resume_service.latest_resume(job_id)
        ctx["has_profile"] = profile_service.has_profile_facts(uid)
        # Side-effect-free (raise-only, no ledger write) — safe to call
        # speculatively so the tab explains a spent daily brake BEFORE the user
        # clicks Generate, not only after a failed POST.
        try:
            usage.check(uid, "resume")
            ctx["resume_blocked"] = False
            ctx["resume_blocked_message"] = None
        except usage.QuotaExceeded as exc:
            ctx["resume_blocked"] = True
            ctx["resume_blocked_message"] = str(exc)
    elif tab == "study":
        ctx["guide"] = study_service.latest_guide(job_id)
        ctx["topic_stats"] = study_service.topic_drill_stats(job_id, uid)
    elif tab == "pulse":
        ctx.update(pulse_service.tab_context(job, uid))
    elif tab == "sessions":
        ctx["sessions"] = interviews_service.list_sessions(job_id, user_id=uid)
    elif tab == "activity":
        ctx["events"] = tracking_service.list_events(job_id)
        ctx["follow_ups"] = tracking_service.open_follow_ups(job_id, user_id=uid)
    elif tab == "overview":
        ctx["posting_doc"] = jobs_service.posting_document(job_id)
    return ctx


def _toast_headers(message: str, tone: str = "success") -> dict:
    return {"HX-Trigger": json.dumps({"toast": {"message": message, "tone": tone}})}


def _tab_response(request: Request, job_id: int, tab: str, message: str):
    """Render the tab partial with a toast for htmx posts; 303 for plain forms."""
    if "hx-request" not in request.headers:
        suffix = f"?tab={tab}" if tab != "overview" else ""
        return RedirectResponse(f"/app/jobs/{public_id_of('jobs', job_id)}{suffix}", status_code=303)
    ctx = _tab_context(request, job_id, tab)
    if ctx is None:
        return HTMLResponse("")
    return templates.TemplateResponse(
        request, f"partials/tab_{tab}.html", ctx, headers=_toast_headers(message)
    )


@router.get("/{job_pid}")
def job_detail(request: Request, tab: str = "overview", job_id: int = Depends(owned_job)):
    tab = tab if tab in JOB_TABS else "overview"
    ctx = _tab_context(request, job_id, tab)
    if ctx is None:
        return RedirectResponse("/app/jobs", status_code=303)
    ctx.update({"active_nav": "jobs", "tab": tab})
    return templates.TemplateResponse(request, "job_detail.html", ctx)


@router.post("/{job_pid}/delete")
def delete_job(request: Request, background: BackgroundTasks, job_id: int = Depends(owned_job)):
    from app.services import insights as insights_service

    name = jobs_service.delete_job(job_id, current_user_id(request))
    if name is None:
        # Already gone — clear the row (htmx) or reload the list.
        if "hx-request" in request.headers:
            return HTMLResponse("")
        return RedirectResponse("/app/jobs", status_code=303)
    # Mixer/global sessions that drew only from this job are now empty (their
    # questions cascaded); clear those orphans so they don't linger as 0/0.
    interviews_service.prune_empty_sessions(current_user_id(request))
    # Removing a job changes the cross-job picture.
    background.add_task(insights_service.request_refresh, current_user_id(request))
    if "hx-request" not in request.headers:
        return RedirectResponse("/app/jobs", status_code=303)
    # Empty body + outerHTML swap removes the row; toast confirms.
    return HTMLResponse("", headers=_toast_headers(f"Removed {name}."))


@router.get("/{job_pid}/row")
def job_row(request: Request, job_id: int = Depends(owned_job)):
    uid = current_user_id(request)
    job = jobs_service.get_job_row(job_id, uid)
    if job is None:
        return HTMLResponse("")
    return templates.TemplateResponse(
        request,
        "partials/job_row.html",
        {"job": job, "has_profile": profile_service.has_profile_facts(uid)},
    )


@router.get("/{job_pid}/tab/{tab}")
def job_tab(request: Request, tab: str, job_id: int = Depends(owned_job)):
    tab = tab if tab in JOB_TABS else "overview"
    ctx = _tab_context(request, job_id, tab)
    if ctx is None:
        return HTMLResponse("")
    return templates.TemplateResponse(request, f"partials/tab_{tab}.html", ctx)


@router.get("/{job_pid}/posting/download")
def download_posting(request: Request, job_id: int = Depends(owned_job)):
    """Give back exactly what the user provided: the original uploaded file, or
    the pasted text as a .txt attachment."""
    from fastapi.responses import PlainTextResponse, Response

    from app.services.storage import get_storage

    doc = jobs_service.posting_document(job_id)
    if doc is not None:
        try:
            content = get_storage().read(doc["path"])
        except FileNotFoundError:
            content = None
        if content is not None:
            safe_name = (doc["filename"] or "posting").replace('"', "")
            return Response(
                content,
                media_type=doc["mime_type"] or "application/octet-stream",
                headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
            )
    job, _, _ = jobs_service.get_job(job_id, current_user_id(request))
    return PlainTextResponse(
        (job["raw_posting"] if job else "") or "",
        headers={"Content-Disposition": 'attachment; filename="posting.txt"'},
    )


@router.post("/{job_pid}/study/generate")
def generate_study(request: Request, background: BackgroundTasks, job_id: int = Depends(owned_job)):
    from app.db import get_conn

    usage.spend(current_user_id(request), "study_guide")
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE jobs SET study_status = 'running', study_error = NULL, "
            "busy_since = datetime('now') WHERE id = ?",
            (job_id,),
        )
        conn.commit()
    finally:
        conn.close()
    background.add_task(study_service.generate_guide, job_id)
    ctx = _tab_context(request, job_id, "study")
    return templates.TemplateResponse(request, "partials/tab_study.html", ctx)


@router.post("/{job_pid}/study/drill")
def study_drill(
    request: Request,
    background: BackgroundTasks,
    topic_index: int = Form(...),
    job_id: int = Depends(owned_job),
):
    """Launch an impromptu one-question drill on a single study-guide topic. The
    button posts the topic's index into the current guide; we read the topic from
    the stored guide, create a 'study' session, and send the user into it."""
    uid = current_user_id(request)
    study_url = f"/app/jobs/{public_id_of('jobs', job_id)}?tab=study"
    guide = study_service.latest_guide(job_id)
    if guide is None:
        return RedirectResponse(study_url, status_code=303)
    topics = json.loads(guide["content_json"]).get("topics", [])
    if not 0 <= topic_index < len(topics):
        return RedirectResponse(study_url, status_code=303)
    t = topics[topic_index]
    usage.spend(uid, "drill")
    session_id = interviews_service.create_study_drill(
        job_id, t.get("topic") or "Study drill", user_id=uid
    )
    if session_id is None:
        return RedirectResponse(study_url, status_code=303)
    background.add_task(
        interviews_service.build_study_drill,
        session_id,
        job_id,
        t.get("topic", ""),
        t.get("why_it_matters", ""),
        t.get("how_it_will_be_tested", ""),
    )
    return RedirectResponse(
        f"/app/study/practice/{public_id_of('interview_sessions', session_id)}", status_code=303
    )


@router.post("/{job_pid}/pulse")
def pulse_request(request: Request, background: BackgroundTasks, job_id: int = Depends(owned_job)):
    """Investigate this job's company, or refresh/retry an existing pulse. The
    service enforces the per-day search allowance server-side regardless of
    what the client rendered. There is no cooldown — a ready pulse can be
    refreshed whenever you want."""
    uid = current_user_id(request)
    job, _, _ = jobs_service.get_job(job_id, uid)
    outcome, pulse_id = pulse_service.request_pulse(job["company"] if job else None, uid)
    if outcome in ("created", "refreshing") and pulse_id is not None:
        background.add_task(pulse_service.submit_pulse, pulse_id)
    message, tone = {
        "created": ("Investigating — this can take a few minutes.", "success"),
        "refreshing": ("Refreshing the pulse — this can take a few minutes.", "success"),
        "busy": ("Already researching this company.", "success"),
        "limit": ("Daily research limit reached — try again tomorrow.", "error"),
        "invalid": ("No researchable company name on this job.", "error"),
    }[outcome]
    if "hx-request" not in request.headers:
        return RedirectResponse(f"/app/jobs/{public_id_of('jobs', job_id)}?tab=pulse", status_code=303)
    ctx = _tab_context(request, job_id, "pulse")
    if ctx is None:
        return HTMLResponse("")
    return templates.TemplateResponse(
        request, "partials/tab_pulse.html", ctx,
        headers={"HX-Trigger": json.dumps({"toast": {"message": message, "tone": tone}})},
    )


@router.post("/{job_pid}/pitch")
def generate_pitch(request: Request, background: BackgroundTasks, job_id: int = Depends(owned_job)):
    from app.db import get_conn

    # check → claim → record, like /analyze: a lost claim isn't charged.
    usage.check(current_user_id(request), "pitch")
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE jobs SET pitch_status = 'running', pitch_error = NULL, "
            "busy_since = datetime('now') WHERE id = ? AND pitch_status <> 'running'",
            (job_id,),
        )
        claimed = cur.rowcount == 1
        conn.commit()
    finally:
        conn.close()
    if claimed:
        usage.record(current_user_id(request), "pitch")
        background.add_task(pitch_service.run_pitch, job_id)
    ctx = _tab_context(request, job_id, "pitch")
    return templates.TemplateResponse(request, "partials/tab_pitch.html", ctx)


def _resume_filename(job) -> str:
    """Build a Content-Disposition filename from LLM-extracted company/title
    text. Unlike download_posting's filename (an uploaded file's own name, or
    a hardcoded 'posting.txt'), company/title are untrusted freeform text —
    strip path separators and CR/LF (header-injection / path-traversal), not
    just the quote download_posting strips."""
    parts = [p for p in (job["company"], job["title"]) if p]
    base = " - ".join(["resume", *parts]) if parts else "resume"
    return re.sub(r'[\\/:*?"<>|\r\n]', "", base).strip() or "resume"


@router.post("/{job_pid}/resume")
def generate_resume(request: Request, background: BackgroundTasks, job_id: int = Depends(owned_job)):
    from app.db import get_conn

    # check -> claim -> record, like /pitch: a lost claim isn't charged.
    usage.check(current_user_id(request), "resume")
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE jobs SET resume_status = 'running', resume_error = NULL, "
            "busy_since = datetime('now') WHERE id = ? AND resume_status <> 'running'",
            (job_id,),
        )
        claimed = cur.rowcount == 1
        conn.commit()
    finally:
        conn.close()
    if claimed:
        usage.record(current_user_id(request), "resume")
        background.add_task(resume_service.run_resume, job_id)
    ctx = _tab_context(request, job_id, "resume")
    return templates.TemplateResponse(request, "partials/tab_resume.html", ctx)


@router.get("/{job_pid}/resume/view")
def view_resume(request: Request, job_id: int = Depends(owned_job)):
    """Standalone printable page in a new tab — its own <html>, not the app
    shell (see resume_print.html)."""
    resume = resume_service.latest_resume(job_id)
    if resume is None:
        raise HTTPException(status_code=404)
    job, _, _ = jobs_service.get_job(job_id, current_user_id(request))
    user = users_service.get_user(current_user_id(request))
    return templates.TemplateResponse(
        request,
        "resume_print.html",
        {"content": json.loads(resume["content_json"]), "job": job, "user": user, "printable": True},
    )


@router.get("/{job_pid}/resume/download")
def download_resume(request: Request, format: str = "md", job_id: int = Depends(owned_job)):
    from fastapi.responses import PlainTextResponse, Response

    resume = resume_service.latest_resume(job_id)
    if resume is None:
        raise HTTPException(status_code=404)
    uid = current_user_id(request)
    job, _, _ = jobs_service.get_job(job_id, uid)
    user = users_service.get_user(uid)
    content = json.loads(resume["content_json"])
    name = _resume_filename(job)
    if format == "html":
        return Response(
            resume_service.render_html(content, job, user, printable=False),
            media_type="text/html; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{name}.html"'},
        )
    if format == "txt":
        return PlainTextResponse(
            resume_service.render_text(content, job, user),
            headers={"Content-Disposition": f'attachment; filename="{name}.txt"'},
        )
    return Response(
        resume_service.render_markdown(content, job, user),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}.md"'},
    )


@router.post("/{job_pid}/analyze")
def rerun_analysis(request: Request, background: BackgroundTasks, job_id: int = Depends(owned_job)):
    from app.db import get_conn

    # check → claim → record: a lost claim (double-submit no-op) isn't charged.
    usage.check(current_user_id(request), "fit")
    conn = get_conn()
    try:
        # Atomic claim: only the first of N rapid double-submits transitions the
        # row and enqueues a task — the rest see 'running' and no-op (A06 / cost).
        cur = conn.execute(
            "UPDATE jobs SET analysis_status = 'running', analysis_error = NULL, "
            "busy_since = datetime('now') WHERE id = ? AND analysis_status <> 'running'",
            (job_id,),
        )
        claimed = cur.rowcount == 1
        conn.commit()
    finally:
        conn.close()
    if claimed:
        usage.record(current_user_id(request), "fit")
        background.add_task(analysis_service.run_fit_analysis, job_id)
    ctx = _tab_context(request, job_id, "fit")
    return templates.TemplateResponse(request, "partials/tab_fit.html", ctx)


@router.post("/{job_pid}/reextract")
def rerun_intake(
    request: Request, background: BackgroundTasks, render: str = "tab",
    job_id: int = Depends(owned_job),
):
    from app.db import get_conn

    usage.check(current_user_id(request), "intake")
    conn = get_conn()
    try:
        # Atomic claim (see rerun_analysis): dedupe double-submits.
        cur = conn.execute(
            "UPDATE jobs SET extract_status = 'extracting', extract_error = NULL, "
            "busy_since = datetime('now') WHERE id = ? AND extract_status <> 'extracting'",
            (job_id,),
        )
        claimed = cur.rowcount == 1
        conn.commit()
    finally:
        conn.close()
    if claimed:
        usage.record(current_user_id(request), "intake")
        background.add_task(jobs_service.run_intake, job_id)
    if render == "row":
        uid = current_user_id(request)
        return templates.TemplateResponse(
            request,
            "partials/job_row.html",
            {"job": jobs_service.get_job_row(job_id, uid),
             "has_profile": profile_service.has_profile_facts(uid)},
        )
    ctx = _tab_context(request, job_id, "overview")
    return templates.TemplateResponse(request, "partials/tab_overview.html", ctx)


@router.post("/{job_pid}/events/interview")
def log_interview(
    request: Request,
    occurred_at: str = Form(""),
    round: str = Form(""),
    format: str = Form(""),
    how_it_went: str = Form(""),
    notes: str = Form(""),
    job_id: int = Depends(owned_job),
):
    tracking_service.log_event(
        job_id,
        "interview",
        {"round": round, "format": format, "how_it_went": how_it_went, "notes": notes},
        occurred_at=occurred_at or None,
    )
    return _tab_response(request, job_id, "activity", "Interview logged")


@router.post("/{job_pid}/events/note")
def log_note(request: Request, text: str = Form(...), job_id: int = Depends(owned_job)):
    tracking_service.log_event(job_id, "note", {"text": text})
    return _tab_response(request, job_id, "activity", "Note added")


@router.post("/{job_pid}/events/feedback")
def log_feedback(request: Request, text: str = Form(...), job_id: int = Depends(owned_job)):
    tracking_service.log_event(job_id, "feedback", {"text": text})
    return _tab_response(request, job_id, "activity", "Feedback added — it feeds study guides")


@router.post("/{job_pid}/followups")
def add_follow_up(
    request: Request, due_at: str = Form(...), reason: str = Form(...),
    job_id: int = Depends(owned_job),
):
    tracking_service.create_follow_up(job_id, due_at, reason)
    return _tab_response(request, job_id, "activity", "Follow-up added")


@router.post("/{job_pid}/tracking")
def update_tracking(
    request: Request,
    status: str = Form(...),
    interest_level: str = Form(""),
    interest_why: str = Form(""),
    url: str = Form(""),
    applied_at: str = Form(""),
    outcome: str = Form(""),
    job_id: int = Depends(owned_job),
):
    from app.text import safe_external_url

    # interest_level arrives as free-form form text; a non-numeric value must
    # not 500 the handler (A10 Mishandling of Exceptional Conditions).
    try:
        interest = int(interest_level) if interest_level.strip() else None
    except ValueError:
        interest = None
    jobs_service.update_tracking(
        job_id,
        user_id=current_user_id(request),
        status=status,
        interest_level=interest,
        interest_why=interest_why,
        url=safe_external_url(url) or "",
        applied_at=applied_at,
        outcome=outcome,
    )
    return _tab_response(request, job_id, "overview", "Tracking saved")
