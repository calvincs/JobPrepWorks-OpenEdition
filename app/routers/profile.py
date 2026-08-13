import json

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from app.identity import current_user_id
from app.config import MAX_UPLOAD_BYTES
from app.db import get_conn, resolve_id
from app.services import analysis as analysis_service
from app.services import documents as documents_service
from app.services import profile as profile_service
from app.services import usage
from app.web import templates

router = APIRouter(prefix="/profile")


def _hx_trigger(**events) -> dict:
    return {"HX-Trigger": json.dumps(events)}


def owned_document(request: Request, document_pid: str) -> int:
    """Resolve a document's public id to its internal id, verifying ownership."""
    did = resolve_id("documents", document_pid, where="AND user_id = ?", params=(current_user_id(request),))
    if did is None:
        raise HTTPException(status_code=404)
    return did


def owned_fact(request: Request, fact_pid: str) -> int:
    """Resolve a fact's public id to its internal id, verifying ownership."""
    fid = resolve_id("profile_facts", fact_pid, where="AND user_id = ?", params=(current_user_id(request),))
    if fid is None:
        raise HTTPException(status_code=404)
    return fid


def owned_parse(request: Request, parse_pid: str) -> int:
    """Resolve a fact-parse's public id to its internal id, verifying ownership."""
    pid = resolve_id("fact_parses", parse_pid, where="AND user_id = ?", params=(current_user_id(request),))
    if pid is None:
        raise HTTPException(status_code=404)
    return pid


def _facts_context(request: Request) -> dict:
    """Everything partials/facts_section.html renders: the grouped facts plus
    any in-flight/errored free-text parses (their banners live in the section)."""
    return {
        "fact_groups": profile_service.facts_grouped(current_user_id(request)),
        "fact_parses": profile_service.fact_parses_pending(current_user_id(request)),
    }


def _document(document_id: int, user_id: int):
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT * FROM documents WHERE id = ? AND user_id = ?", (document_id, user_id)
        ).fetchone()
    finally:
        conn.close()


def _page_context(request: Request, upload_error: str | None = None) -> dict:
    uid = current_user_id(request)
    conn = get_conn()
    try:
        docs = conn.execute(
            "SELECT * FROM documents WHERE user_id = ? AND purpose = 'profile' ORDER BY uploaded_at DESC",
            (uid,),
        ).fetchall()
    finally:
        conn.close()
    return {
        "active_nav": "profile",
        "documents": docs,
        "upload_error": upload_error,
        **_facts_context(request),
        **_direction_card_context(request),
    }


@router.get("")
def profile_page(request: Request):
    return templates.TemplateResponse(request, "profile.html", _page_context(request))


@router.post("/documents")
def upload_document(request: Request, background: BackgroundTasks, file: UploadFile):
    # Sync (`def`) on purpose: the multipart body is already spooled, and the
    # disk write + DB insert below would block the event loop as `async def`.
    # Ledger before the upload lands: a refusal must leave no file or doc row.
    usage.spend(current_user_id(request), "document")
    # Bounded read: cap memory/disk before save_upload validates (A06/LLM10).
    content = file.file.read(MAX_UPLOAD_BYTES + 1)
    try:
        document_id = documents_service.save_upload(
            file.filename or "upload", content, user_id=current_user_id(request)
        )
    except (documents_service.UnsupportedFileType, documents_service.FileTooLarge) as exc:
        return templates.TemplateResponse(
            request, "profile.html", _page_context(request, upload_error=str(exc)), status_code=422
        )
    background.add_task(profile_service.process_document, document_id)
    return RedirectResponse("/app/profile", status_code=303)


@router.get("/documents/{document_pid}/row")
def document_row(request: Request, document_id: int = Depends(owned_document)):
    doc = _document(document_id, current_user_id(request))
    if doc is None:
        return HTMLResponse("")
    # live=True lets the row trigger a facts-section refresh when extraction lands
    return templates.TemplateResponse(
        request, "partials/document_row.html", {"doc": doc, "live": True}
    )


@router.get("/facts-section")
def facts_section(request: Request):
    return templates.TemplateResponse(
        request, "partials/facts_section.html", _facts_context(request)
    )


def _direction_card_context(request: Request) -> dict:
    uid = current_user_id(request)
    facts = profile_service.direction_facts(uid)
    answered = sum(
        1 for name, _q, _p in profile_service.DIRECTION_STEPS
        if facts.get(name) and facts[name]["detail"]
    )
    return {"direction_answered": answered, "direction_total": len(profile_service.DIRECTION_STEPS)}


def _direction_step_context(request: Request, n: int) -> dict:
    steps = profile_service.DIRECTION_STEPS
    name, question, placeholder = steps[n - 1]
    facts = profile_service.direction_facts(current_user_id(request))
    answered = {
        i for i, (nm, _q, _p) in enumerate(steps, start=1)
        if facts.get(nm) and facts[nm]["detail"]
    }
    row = facts.get(name)
    return {
        "step_num": n,
        "total": len(steps),
        "title": name,
        "question": question,
        "placeholder": placeholder,
        "answer": (row["detail"] if row else "") or "",
        "answered": answered,
    }


def _valid_step(n: int) -> None:
    if not 1 <= n <= len(profile_service.DIRECTION_STEPS):
        raise HTTPException(status_code=404)


@router.get("/direction/card")
def direction_card(request: Request):
    return templates.TemplateResponse(
        request, "partials/direction_card.html", _direction_card_context(request)
    )


@router.get("/direction/step/{n}")
def direction_step(request: Request, n: int):
    _valid_step(n)
    return templates.TemplateResponse(
        request, "partials/direction_step.html", _direction_step_context(request, n)
    )


@router.post("/direction/step/{n}")
def save_direction_step(request: Request, n: int, answer: str = Form("")):
    """Save one wizard answer (blank = skip-with-submit, a no-op) and serve the
    next step; the last step closes the dialog and refreshes the facts list."""
    _valid_step(n)
    profile_service.save_direction_answer(current_user_id(request),
        profile_service.DIRECTION_STEPS[n - 1][0], answer)
    if n < len(profile_service.DIRECTION_STEPS):
        return templates.TemplateResponse(
            request, "partials/direction_step.html", _direction_step_context(request, n + 1)
        )
    return HTMLResponse(
        "",
        headers=_hx_trigger(
            toast={"message": "Career direction saved.", "tone": "success"},
            **{"close-dialog": True, "refresh-facts": True},
        ),
    )


@router.get("/facts/{fact_pid}/row")
def fact_row(request: Request, fact_id: int = Depends(owned_fact)):
    fact = profile_service.fact_with_sources(fact_id, current_user_id(request))
    if fact is None:
        return HTMLResponse("")
    return templates.TemplateResponse(request, "partials/fact_row.html", {"fact": fact})


@router.post("/facts/unsourced/remove")
def remove_unsourced(request: Request):
    n = profile_service.remove_unsourced_facts(current_user_id(request))
    plural = "s" if n != 1 else ""
    return templates.TemplateResponse(
        request,
        "partials/facts_section.html",
        _facts_context(request),
        headers=_hx_trigger(toast={"message": f"Removed {n} unsourced fact{plural}.", "tone": "success"}),
    )


@router.post("/documents/{document_pid}/reextract")
def reextract_document(
    request: Request, background: BackgroundTasks, document_id: int = Depends(owned_document)
):
    uid = current_user_id(request)
    usage.spend(uid, "document")
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE documents SET status = 'parsing', error = NULL, "
            "busy_since = datetime('now') WHERE id = ? AND user_id = ?",
            (document_id, uid),
        )
        conn.commit()
    finally:
        conn.close()
    background.add_task(profile_service.process_document, document_id)
    doc = _document(document_id, uid)
    return templates.TemplateResponse(request, "partials/document_row.html", {"doc": doc})


@router.post("/documents/{document_pid}/delete")
def delete_document(request: Request, document_id: int = Depends(owned_document)):
    uid = current_user_id(request)
    doc = _document(document_id, uid)
    name = doc["filename"] if doc else "Document"
    unsourced = documents_service.delete_document(document_id, uid)
    msg = f"Removed {name}."
    if unsourced:
        plural = "s" if unsourced != 1 else ""
        msg = f"Removed {name}. {unsourced} fact{plural} now unsourced."
    # Non-HTMX fallback (e.g. JS disabled): full reload.
    if "hx-request" not in request.headers:
        return RedirectResponse("/app/profile", status_code=303)
    # Empty body + outerHTML swap removes the row; refresh-facts re-renders the
    # facts section (unsourced banner); toast confirms.
    return HTMLResponse(
        "",
        headers=_hx_trigger(
            toast={"message": msg, "tone": "success"},
            **{"refresh-facts": True},
        ),
    )


@router.post("/facts")
def create_fact(
    request: Request,
    background: BackgroundTasks,
    kind: str = Form(...),
    name: str = Form(...),
    organization: str = Form(""),
    detail: str = Form(""),
    proficiency: str = Form(""),
    start_date: str = Form(""),
    end_date: str = Form(""),
    origin: str = Form(""),
    job_pid: str = Form(""),
    gap_requirement: str = Form(""),
):
    """Manually add a profile fact (no document behind it). origin='gap' means
    the post came from the Fit tab's gap dialog — that page has no
    #facts-section to re-render, so the answer is a toast, plus a gap-resolved
    event (checking the gap off on the tab) when job_pid/gap_requirement name
    a real gap of the job's latest analysis."""
    if "hx-request" not in request.headers:
        # Modal is JS-only; a no-JS post just bounces back to the page.
        return RedirectResponse("/app/profile", status_code=303)
    if kind not in profile_service.VALID_KINDS or not name.strip():
        # The UI enforces both (required name, fixed select) — this is defensive.
        if origin == "gap":
            return HTMLResponse(
                "", headers=_hx_trigger(toast={"message": "Pick a type and enter a name.", "tone": "error"})
            )
        return templates.TemplateResponse(
            request,
            "partials/facts_section.html",
            _facts_context(request),
            headers=_hx_trigger(toast={"message": "Pick a type and enter a name.", "tone": "error"}),
        )
    uid = current_user_id(request)
    was_empty = not profile_service.has_profile_facts(uid)
    _fact_id, action = profile_service.create_manual_fact(
        user_id=uid,
        kind=kind,
        name=name,
        organization=organization,
        detail=detail,
        proficiency=proficiency,
        start_date=start_date,
        end_date=end_date,
    )
    # First fact on an empty profile — score the jobs that were waiting on it.
    if was_empty and profile_service.has_profile_facts(uid):
        background.add_task(analysis_service.reanalyze_all_jobs, uid)
    msg = f"Added {name.strip()}." if action == "created" else f"Merged into existing {name.strip()}."
    if origin == "gap":
        events = {
            "toast": {"message": f"{msg} Re-analyze to update this job's fit.", "tone": "success"},
            "close-dialog": True,
        }
        # Check the gap off on the Fit tab. A missing/forged job_pid or a
        # requirement that isn't a gap of the latest analysis is a silent
        # no-op — the fact itself already landed, so never fail the request;
        # the tab just doesn't refresh.
        if job_pid:
            jid = resolve_id("jobs", job_pid, where="AND user_id = ?", params=(uid,))
            if jid is not None and analysis_service.resolve_gap(
                jid, gap_requirement or name, user_id=uid
            ):
                events["gap-resolved"] = True
        return HTMLResponse("", headers=_hx_trigger(**events))
    return templates.TemplateResponse(
        request,
        "partials/facts_section.html",
        _facts_context(request),
        headers=_hx_trigger(
            toast={"message": msg, "tone": "success"},
            **{"close-dialog": True},
        ),
    )


@router.post("/facts/parse")
def parse_facts(request: Request, background: BackgroundTasks, text: str = Form(""), origin: str = Form("")):
    """Freeform 'describe it' fact intake: record the text, parse it into facts
    in the background, and let the facts-section banner poll for the result.
    origin='gap' (the Fit tab's gap dialog) gets just the self-polling banner —
    that page has no #facts-section; the form swaps it into #gap-parse-host."""
    if "hx-request" not in request.headers:
        # Modal is JS-only; a no-JS post just bounces back to the page.
        return RedirectResponse("/app/profile", status_code=303)
    if not text.strip():  # required in the UI — defensive
        if origin == "gap":
            return HTMLResponse(
                "", headers=_hx_trigger(toast={"message": "Describe the fact first.", "tone": "error"})
            )
        return templates.TemplateResponse(
            request,
            "partials/facts_section.html",
            _facts_context(request),
            headers=_hx_trigger(toast={"message": "Describe the fact first.", "tone": "error"}),
        )
    uid = current_user_id(request)
    usage.spend(uid, "fact_parse")
    parse_id = profile_service.create_fact_parse(text, user_id=uid)
    background.add_task(profile_service.process_fact_parse, parse_id)
    if origin == "gap":
        return templates.TemplateResponse(
            request,
            "partials/fact_parse_banner.html",
            {"p": profile_service.fact_parse(parse_id, uid)},
            headers=_hx_trigger(**{"close-dialog": True}),
        )
    # The re-rendered section now carries this parse's self-polling banner.
    return templates.TemplateResponse(
        request,
        "partials/facts_section.html",
        _facts_context(request),
        headers=_hx_trigger(**{"close-dialog": True}),
    )


@router.get("/facts/parses/{parse_pid}")
def fact_parse_status(request: Request, parse_id: int = Depends(owned_parse)):
    """Poll target for a parse banner. Terminal 'ready' deletes the transient
    row and returns an empty swap (removing the banner); refresh-facts makes
    the section re-render with the new facts, and the summary rides a toast."""
    uid = current_user_id(request)
    p = profile_service.fact_parse(parse_id, uid)
    if p is None:
        return HTMLResponse("")
    if p["status"] == "ready":
        profile_service.delete_fact_parse(parse_id, uid)
        return HTMLResponse(
            "",
            headers=_hx_trigger(
                toast={"message": p["summary"] or "Facts added.", "tone": "success"},
                **{"refresh-facts": True},
            ),
        )
    return templates.TemplateResponse(request, "partials/fact_parse_banner.html", {"p": p})


@router.post("/facts/parses/{parse_pid}/dismiss")
def dismiss_fact_parse(request: Request, parse_id: int = Depends(owned_parse)):
    """Dismiss an errored parse banner (empty swap removes it)."""
    profile_service.delete_fact_parse(parse_id, current_user_id(request))
    return HTMLResponse("")


@router.get("/facts/{fact_pid}/edit")
def edit_fact_form(request: Request, fact_id: int = Depends(owned_fact)):
    conn = get_conn()
    try:
        fact = conn.execute("SELECT * FROM profile_facts WHERE id = ?", (fact_id,)).fetchone()
    finally:
        conn.close()
    if fact is None:
        return HTMLResponse("")
    return templates.TemplateResponse(request, "partials/fact_edit.html", {"fact": fact})


@router.post("/facts/{fact_pid}")
def update_fact(
    request: Request,
    name: str = Form(...),
    organization: str = Form(""),
    detail: str = Form(""),
    proficiency: str = Form(""),
    kind: str = Form(""),
    start_date: str = Form(""),
    end_date: str = Form(""),
    fact_id: int = Depends(owned_fact),
):
    if not name.strip():  # required in the UI — defensive
        return HTMLResponse("", status_code=400)
    uid = current_user_id(request)
    profile_service.update_fact(
        fact_id, user_id=uid, name=name, organization=organization, detail=detail,
        proficiency=proficiency, kind=kind or None, start_date=start_date, end_date=end_date,
    )
    fact = profile_service.fact_with_sources(fact_id, uid)
    if fact is None:  # not owned
        return HTMLResponse("")
    # The edit UI is a modal; close it once the row swaps in.
    return templates.TemplateResponse(
        request, "partials/fact_row.html", {"fact": fact},
        headers=_hx_trigger(toast={"message": f"Updated {name.strip()}.", "tone": "success"},
                            **{"close-dialog": True}),
    )


@router.post("/facts/{fact_pid}/delete")
def delete_fact(request: Request, fact_id: int = Depends(owned_fact)):
    profile_service.delete_fact(fact_id, current_user_id(request))
    return HTMLResponse("")
