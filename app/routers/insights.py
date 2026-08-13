from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.identity import current_user_id
from app.db import resolve_id
from app.services import insights as insights_service
from app.services import usage
from app.web import templates

router = APIRouter(prefix="/insights")


def owned_insight(request: Request, insight_pid: str) -> int:
    """Resolve an insight's public id to its internal id, scoped to the owner."""
    iid = resolve_id("insights", insight_pid, where="AND user_id = ?", params=(current_user_id(request),))
    if iid is None:
        raise HTTPException(status_code=404)
    return iid


def _page_context(request: Request) -> dict:
    user_id = current_user_id(request)
    return {
        "active_nav": "insights",
        "insights": insights_service.list_insights(user_id),
        **insights_service.page_status(user_id),
    }


@router.get("")
def insights_page(request: Request):
    return templates.TemplateResponse(request, "insights.html", {**_page_context(request)})


@router.get("/list")
def insights_list(request: Request):
    return templates.TemplateResponse(request, "partials/insights_list.html", _page_context(request))


@router.post("/refresh")
def refresh(request: Request, background: BackgroundTasks):
    # Claim synchronously so the returned partial already polls as 'running';
    # generation itself happens after the response. A failed claim means a run
    # is in flight — the partial shows that run's state.
    user_id = current_user_id(request)
    # check → claim → record: a lost claim (run already in flight) isn't charged.
    usage.check(user_id, "insights")
    insights_service.mark_stale(user_id)
    run_id = insights_service.claim_run(user_id)
    if run_id is not None:
        usage.record(user_id, "insights")
        background.add_task(insights_service.run_claimed, user_id, run_id)
    return templates.TemplateResponse(request, "partials/insights_list.html", _page_context(request))


@router.post("/{insight_pid}/dismiss")
def dismiss(request: Request, insight_id: int = Depends(owned_insight)):
    insights_service.dismiss(insight_id, current_user_id(request))
    return HTMLResponse("")
