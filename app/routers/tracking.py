import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from app.identity import current_user_id
from app.db import resolve_id
from app.services import tracking as tracking_service
from app.web import templates

router = APIRouter(prefix="/followups")


def _toast_headers(message: str) -> dict:
    return {"HX-Trigger": json.dumps({"toast": {"message": message, "tone": "success"}})}


def owned_follow_up(request: Request, follow_up_pid: str) -> int:
    """Resolve a follow-up's public id to its internal id, scoped to the acting
    user via its job's owner; 404 otherwise."""
    fid = resolve_id(
        "follow_ups", follow_up_pid,
        where="AND job_id IN (SELECT id FROM jobs WHERE user_id = ?)",
        params=(current_user_id(request),),
    )
    if fid is None:
        raise HTTPException(status_code=404)
    return fid


@router.get("/panel")
def panel(request: Request):
    return templates.TemplateResponse(
        request,
        "partials/followups_panel.html",
        {"follow_ups": tracking_service.open_follow_ups(user_id=current_user_id(request))},
    )


@router.post("/{follow_up_pid}/resolve")
def resolve(request: Request, resolution: str = Form(...), follow_up_id: int = Depends(owned_follow_up)):
    tracking_service.resolve_follow_up(follow_up_id, resolution, current_user_id(request))
    return templates.TemplateResponse(
        request,
        "partials/followups_panel.html",
        {"follow_ups": tracking_service.open_follow_ups(user_id=current_user_id(request))},
        headers=_toast_headers("Follow-up resolved"),
    )


@router.post("/{follow_up_pid}/snooze")
def snooze(request: Request, follow_up_id: int = Depends(owned_follow_up)):
    tracking_service.snooze_follow_up(follow_up_id, user_id=current_user_id(request))
    return templates.TemplateResponse(
        request,
        "partials/followups_panel.html",
        {"follow_ups": tracking_service.open_follow_ups(user_id=current_user_id(request))},
        headers=_toast_headers("Snoozed 7 days"),
    )
