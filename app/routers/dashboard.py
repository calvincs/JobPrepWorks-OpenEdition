from fastapi import Request

from app.identity import current_user_id
from app.db import get_conn
from app.services import gamification
from app.services import insights as insights_service
from app.services import jobs as jobs_service
from app.services import tracking as tracking_service
from app.web import templates

JOB_STATUSES = ["researching", "training", "applied", "interviewing", "offer", "rejected", "withdrawn"]

# The Recent-jobs card shows the top N under the chosen sort (newest first by
# default — same whitelisted columns as the Jobs page).
RECENT_JOBS_LIMIT = 8


# The app shell's index, served at /app. No APIRouter here: an empty path needs
# the wrapper's prefix at registration time, so main.py adds this handler to
# app_router directly (a bare "" path on an unprefixed router is invalid).
def dashboard(request: Request, sort: str = jobs_service.DEFAULT_SORT, dir: str = "desc"):
    uid = current_user_id(request)
    if sort not in jobs_service.SORT_COLUMNS:
        sort = jobs_service.DEFAULT_SORT
    dir = "asc" if dir == "asc" else "desc"
    recent_jobs = jobs_service.list_jobs(sort, dir, user_id=uid, limit=RECENT_JOBS_LIMIT)
    conn = get_conn()
    try:
        status_counts = {
            row["status"]: row["n"]
            for row in conn.execute(
                "SELECT status, COUNT(*) AS n FROM jobs WHERE user_id = ? GROUP BY status",
                (uid,),
            )
        }
        fact_count = conn.execute(
            "SELECT COUNT(*) FROM profile_facts WHERE user_id = ? AND orphaned = 0",
            (uid,),
        ).fetchone()[0]
        doc_count = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE user_id = ? AND purpose = 'profile'",
            (uid,),
        ).fetchone()[0]
    finally:
        conn.close()

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "active_nav": "dashboard",
            "status_counts": status_counts,
            "statuses": JOB_STATUSES,
            "recent_jobs": recent_jobs,
            "sort": sort,
            "dir": dir,
            "fact_count": fact_count,
            "doc_count": doc_count,
            "streak": gamification.current_streak(uid),
            "stats": gamification.session_stats(uid),
            "mastery": gamification.mastery(uid, limit=6),
            "awards": gamification.recent_awards(uid),
            "follow_ups": tracking_service.open_follow_ups(user_id=uid),
            "top_insights": insights_service.list_insights(uid, limit=3),
            "insights_state": insights_service.page_status(uid),
        },
    )
