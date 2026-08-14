import json

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import (
    DB_PATH,
    MOCK,
    UPLOADS_DIR,
    resolved_model,
    research_provider_name,
    search_backend_name,
    settings,
)
from app.identity import current_user_id
from app.services import usage as usage_service
from app.services import users as users_service
from app.web import templates

router = APIRouter(prefix="/account")


def _hx_trigger(**events) -> dict:
    return {"HX-Trigger": json.dumps(events)}


def _provider_ctx() -> dict:
    """What the Settings page reports about the configured model backend. Read
    straight from settings so the page always describes the process you're
    actually running — not what the .env on disk says after an edit.

    The mock provider is reported as such rather than by its (irrelevant)
    search backend: mock serves a canned pulse without touching the network, so
    printing "no web search" next to a working Pulse tab would be a lie."""
    mock = settings.llm_provider == MOCK
    return {
        "provider": settings.llm_provider,
        "model": "(canned responses)" if mock else (resolved_model() or "(not set)"),
        "research_provider": research_provider_name(),
        "research_model": (
            "(canned responses)" if mock
            else (settings.research_model or resolved_model() or "(not set)")
        ),
        "search_backend": "mock" if mock else search_backend_name(),
    }


def _page(request: Request):
    uid = current_user_id(request)
    return templates.TemplateResponse(
        request,
        "account.html",
        {
            "active_nav": "account",
            "user": users_service.get_user(uid),
            "usage": usage_service.usage_summary(uid),
            # Shown so you always know which files hold your data — this is a
            # local tool, so "delete my data" is `rm` on these, not a button.
            "db_path": str(DB_PATH),
            "uploads_path": str(UPLOADS_DIR),
            **_provider_ctx(),
        },
    )


@router.get("")
def account_page(request: Request):
    return _page(request)


@router.post("/name")
def save_name(
    request: Request,
    first_name: str = Form(""),
    last_name: str = Form(""),
    contact_email: str = Form(""),
    contact_phone: str = Form(""),
):
    saved = users_service.set_profile(
        first_name, last_name,
        contact_email=contact_email, contact_phone=contact_phone,
        user_id=current_user_id(request),
    )
    if "hx-request" not in request.headers:
        return RedirectResponse("/app/account", status_code=303)
    if not saved:
        return HTMLResponse(
            "", headers=_hx_trigger(toast={"message": "Enter your first name.", "tone": "error"})
        )
    return HTMLResponse(
        "", headers=_hx_trigger(toast={"message": "Saved.", "tone": "success"})
    )


@router.post("/display")
def save_display(request: Request, theme: str = Form("")):
    """Store the display preference. The response's theme-pref event lets
    app.js apply it to this tab immediately (and cache it in localStorage)."""
    saved = users_service.set_theme(theme, user_id=current_user_id(request))
    if "hx-request" not in request.headers:
        return RedirectResponse("/app/account", status_code=303)
    if not saved:  # the select only offers valid values — defensive
        return HTMLResponse(
            "", headers=_hx_trigger(toast={"message": "Pick a display option.", "tone": "error"})
        )
    return HTMLResponse(
        "",
        headers=_hx_trigger(
            toast={"message": "Display preference saved.", "tone": "success"},
            **{"theme-pref": {"theme": theme}},
        ),
    )
